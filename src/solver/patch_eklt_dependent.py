import logging
from typing import Optional, Tuple, Dict
import math
import cv2
import numpy as np
import torch
import os

from torchvision.transforms.functional import gaussian_blur
from . import scipy_autograd

from .. import solver, visualizer, utils, costs, types
from .patch_eklt import PatchEklt
from .generative_max_likelihood import LossVideosMaker
from ..utils.frame_utils import range_norm


logger = logging.getLogger(__name__)


class PatchEkltDependent(PatchEklt):
    def __init__(
            self,
            orig_image_shape: tuple,
            crop_image_shape: tuple,
            calibration_parameter: dict,
            solver_config: dict = {},
            visualize_module: Optional[visualizer.Visualizer] = None,
    ) -> None:
        """Method to determine Optical flow from frames and events inspired by
        https://www.zora.uzh.ch/id/eprint/197701/1/eklt_ijcv19.pdf on patches
        of the frame. Intermediate pixel values are interpolated from the patch
        results

        Args:
            orig_image_shape:
            crop_image_shape:
            calibration_parameter:
            solver_config:
            visualize_module:
        """
        super().__init__(
            orig_image_shape,
            crop_image_shape,
            calibration_parameter,
            solver_config,
            visualize_module,
        )

    @utils.profile(
        output_file="optimize.prof", sort_by="cumulative", lines_to_print=300, strip_dirs=True
    )
    def estimate(self, events: np.ndarray, *args, **kwargs) -> np.ndarray:
        if self._gml_config["model_image"] == "current":
            self._set_frame(kwargs["frame"])
        elif self._frame is None and self._gml_config["model_image"] == "background":
            self._set_frame(kwargs["background"])

        # estimate coarse flow array
        self.calculate_iwe_cache(events)

        # First, get the number of the parameters to estimate, by ROI and threshloding events.
        self.estimate_indices = []
        for i in range(self.n_patch):
            if self.patches[i].x < self.crop_xmin or self.crop_xmax < self.patches[i].x:
                continue
            if self.patches[i].y < self.crop_ymin or self.crop_ymax < self.patches[i].y:
                continue
            # Window is inside the whole cropping
            cropped = utils.crop_event(
                events,
                self.patches[i].x_min,
                self.patches[i].x_max,
                self.patches[i].y_min,
                self.patches[i].y_max,
            )

            if not self.do_event_thresholding or len(cropped) > self.event_thres:                
                self.estimate_indices.append(i)
        self.estimate_indices = np.array(self.estimate_indices)

        n_parameter_patch = len(self.estimate_indices)
        self.n_parameter_dim = len(self._initialize_velocity())
        x0 = np.concatenate([self._initialize_velocity() for _ in range(n_parameter_patch)])
        x0 = torch.from_numpy(x0).double().to(self._device).requires_grad_()

        roi = {"xmin": self.crop_xmin, "xmax": self.crop_xmax, "ymin": self.crop_ymin, "ymax": self.crop_ymax}
        measured_increment, weights = self._make_measured_increment(events, roi)
        measured_increment = torch.from_numpy(measured_increment).double().to(self._device)
        weights = torch.from_numpy(weights).double().to(self._device) if weights is not None else weights

        # torch optimizers
        lr_step = iters = self._opt_config["n_iter"]
        # lr, lr_decay = 0.05, 0.1
        lr, lr_decay = 0.05, 0.1
        optimizer = torch.optim.__dict__[self._opt_method]([x0], lr=lr)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, lr_step, lr_decay)
        best_x, best_it, min_loss = x0, 0, math.inf
        for it in range(iters):
            optimizer.zero_grad()
            loss = self._objective_scipy(x0, measured_increment, roi, weights)
            
            # visualize evolution     
            self.visualize_evolution(x0, measured_increment, weights, roi)
            if loss < min_loss:
                best_x = x0
                min_loss = loss.item()
                best_it = it
            try:
                loss.backward()
            except Exception as e:
                logger.error(e)
                break
            optimizer.step()
            scheduler.step()

        best_x = best_x.clone().detach()
        dense_flow = self._extrapolate_dense_flow_from_estimates(best_x)

        self.visualizer.visualize_scipy_history(self.cost_func.get_history())
        self._video_maker.make_video()
        self.cost_func.clear_history()
        
        # # Scipy optimizers
        # result = scipy_autograd.minimize(
        #     lambda x: self._objective_scipy(x, measured_increment, roi, weights),
        #     x0=x0,
        #     method=self._opt_method,
        #     options={"gtol": 1e-8, "disp": True},
        #     precision='float64',
        # )
        # if not result.success:
        #     logger.warning("Unsuccessful optimization step!")
        # dense_flow = self._extrapolate_dense_flow_from_estimates(result.x)
        del self.cache_histogram, self.cache_weights  # free cache
        self.iter_cnt += 1
        return dense_flow.cpu().numpy()

    def visualize_evolution(self, params, measurement, weights, roi):
        if not logger.isEnabledFor(logging.DEBUG):
            return

        if weights is not None:
            weights = weights.clone().detach()
        prediction = self._make_prediction_torch(params, roi, weights).detach().cpu().numpy()

        dense_flow = self._extrapolate_dense_flow_from_estimates(params).clone().detach().cpu().numpy()
        roi_dense_flow = dense_flow[:, self.crop_xmin:self.crop_xmax, self.crop_ymin:self.crop_ymax]
        self._video_maker.visualize_flow(roi_dense_flow, "opt_flow")

        if self._gml_config["optimize_warp"]:
            translation = self._extrapolate_dense_translation_from_estimates(params).clone().detach().cpu().numpy()
            roi_translation = translation[:, self.crop_xmin:self.crop_xmax, self.crop_ymin:self.crop_ymax]
            self._video_maker.visualize_flow(roi_translation, "opt_pxy")
        if self.is_poisson_model:
            # poisson = self._get_patch_poisson(params).clone().detach().cpu().numpy()
            dense_poisson = self._extrapolate_dense_poisson_from_estimates(params).clone().detach().cpu().numpy()
            roi_poisson = dense_poisson[..., self.crop_xmin:self.crop_xmax, self.crop_ymin:self.crop_ymax]
            self._video_maker.visualize_image(range_norm(roi_poisson, dtype=np.uint8),
                                              "opt_poisson")

        measured_increment = measurement.clone().detach().cpu().numpy()
        diff = prediction - measured_increment
        lower, upper = self._gml_config["viz_diff_scale"]
        d_min, d_max = np.min(diff), np.max(diff)
        if d_min < lower:
            logger.warning(f"The lowest value in diff is {d_min} but lower scale is {lower}")
        if d_max > upper:
            logger.warning(f"The highest value in diff is {d_max} but lower scale is {upper}")
        diff = range_norm(diff, lower=lower,
                        upper=upper, dtype=np.uint8)
        self._video_maker.visualize_image(diff, "opt_diff")
        self._video_maker.visualize_image(range_norm(prediction, dtype=np.uint8),
                                        "opt_prediction")
        self._video_maker.visualize_image(range_norm(measured_increment, dtype=np.uint8),
                                        "opt_measured")

    def _extrapolate_dense_flow_from_estimates(self, parameters: torch.Tensor, *args):
        # First interpolation, next Sobel
        # patch_poisson = self._get_patch_poisson(parameters)
        # dense_poisson = self.interpolate_dense_poisson_from_patch_tensor(patch_poisson)
        # dense_flow = self.poisson_to_flow(dense_poisson)
        # First Sobel, next interpolation
        patch_flow = self._get_patch_flow(parameters)
        dense_flow = self.interpolate_dense_flow_from_patch_tensor(patch_flow.reshape((2, ) + self.patch_image_size))
        return dense_flow

    def _get_patch_flow(self, parameters: torch.Tensor):
        """From parameters that has reduced number of patches, recovers the original patch size flow.
        Args:
            parameters ... 1-dimensional, n_dim x n_target_patches
        """
        # print('-=-=-=-', parameters.shape)
        # raise RuntimeError
        # reshaped_params = parameters.reshape(-1, self.n_parameter_dim).T.reshape((1, self.n_parameter_dim) + self.patch_image_size) # 1, n, h, w
        # pad_array = self.get_patch_pad_shape()

        reshaped_params = parameters.reshape(-1, self.n_parameter_dim).T
        if self.is_poisson_model:
            assert self.n_parameter_dim in [1, 3]
            orig_patch_p = torch.zeros((self.n_parameter_dim, self.n_patch)).double().to(self._device)
            orig_patch_p[:, self.estimate_indices] = reshaped_params[0]
            # orig_patch_p = torch.nn.functional.pad(reshaped_params, pad_array, mode="replicate")[0]
            patch_flow = self.poisson_to_flow(orig_patch_p.reshape((self.n_parameter_dim, ) + self.patch_image_size))
        else:
            if self.is_angle_model:
                assert self.n_parameter_dim in [1, 3]
                v_x, v_y = torch.sin(reshaped_params[0]), torch.cos(reshaped_params[0])   # each has n-patch length
            else:
                assert self.n_parameter_dim in [2, 4]
                v_x, v_y = reshaped_params[0], reshaped_params[1]   # each has n-patch length
            patch_flow = torch.zeros((2, self.n_patch)).double().to(self._device)
            patch_flow[0, self.estimate_indices] += v_x
            patch_flow[1, self.estimate_indices] += v_y
            patch_flow = patch_flow.reshape((2, ) + self.patch_image_size)
        return patch_flow

    def _extrapolate_dense_translation_from_estimates(self, parameters: torch.Tensor):
        patch_translation = self._get_patch_translation(parameters)
        dense_translation = self.interpolate_dense_flow_from_patch_tensor(patch_translation)
        return dense_translation

    def _get_patch_translation(self, parameters: torch.Tensor):
        """From parameters that has reduced number of patches, recovers the original patch size translation (px, py).
        """
        reshaped_params = parameters.reshape(-1, self.n_parameter_dim).T   # 3 x patch (or 1 x patch)
        if self.is_poisson_model:
            assert self.n_parameter_dim == 3
            v_x, v_y = reshaped_params[1], reshaped_params[2]   # each has n-patch length
        else:
            if self.is_angle_model:
                assert self.n_parameter_dim == 3
                v_x, v_y = reshaped_params[1], reshaped_params[2]   # each has n-patch length
            else:
                assert self.n_parameter_dim == 4
                v_x, v_y = reshaped_params[2], reshaped_params[3]   # each has n-patch length
        orig_patch_tr = torch.zeros((2, self.n_patch)).double().to(self._device)
        orig_patch_tr[0, self.estimate_indices] += v_x
        orig_patch_tr[1, self.estimate_indices] += v_y
        return orig_patch_tr.reshape((2, ) + self.patch_image_size)

    def _extrapolate_dense_poisson_from_estimates(self, parameters: torch.Tensor):
        patch_poisson = self._get_patch_poisson(parameters)
        dense_poisson = self.interpolate_dense_poisson_from_patch_tensor(patch_poisson)
        return dense_poisson

    def _get_patch_poisson(self, parameters: torch.Tensor):
        """From parameters that has reduced number of patches, recovers the original patch size Poisson (intensity).
        Returns 1, patch_h, patch_w
        """
        reshaped_params = parameters.reshape(-1, self.n_parameter_dim).T   # 3 x patch (or 1 x patch)
        assert self.is_poisson_model
        assert self.n_parameter_dim in [1, 3]
        p = reshaped_params[0]   # each has n-patch length
        orig_patch_ps = torch.zeros((1, self.n_patch)).double().to(self._device)
        orig_patch_ps[0, self.estimate_indices] += p
        return orig_patch_ps.reshape((1, ) + self.patch_image_size)

    def poisson_to_flow(self, poisson, gaussian_sigma=0):
        """Get optical flow from intensity

        Args:
            poisson (_type_): numpy or torch, H x W
        """
        return_numpy = False
        if types.is_numpy(poisson):
            return_numpy = True
            poisson = torch.from_numpy(poisson).double().to(self._device)
        if len(poisson.shape) == 2:
            poisson = poisson[None, None]
        elif len(poisson.shape) == 3:
            poisson = poisson[None]

        # if gaussian_sigma > 0:
        #     poisson = gaussian_blur(poisson, kernel_size=5, sigma=gaussian_sigma)
        flow = utils.SobelTorch(in_channels=1, precision='64', ksize=self.sobel_ksize,
                                padding=self.sobel_padding, cuda_available=self._cuda_available)(poisson) / 8.

        if return_numpy:
            return flow.detach().cpu().numpy()[0]
        return flow[0]

    def _make_prediction_torch(self, parameters: torch.Tensor, roi: dict, weights: torch.Tensor):
        """Overload: optimized version of GenerativeMaximumLikelihood._make_prediction_torch
        """
        x_min, x_max = roi["xmin"], roi["xmax"]
        y_min, y_max = roi["ymin"], roi["ymax"]
        gradient_x = self._gradient_x_torch.clone()[x_min: x_max, y_min: y_max]
        gradient_y = self._gradient_y_torch.clone()[x_min: x_max, y_min: y_max]

        dense_flow = self._extrapolate_dense_flow_from_estimates(parameters)
        roi_dense_flow = dense_flow[:, x_min:x_max, y_min:y_max]

        if self._gml_config["optimize_warp"]:
            translation = self._extrapolate_dense_translation_from_estimates(parameters)
            roi_translation = translation[:, x_min:x_max, y_min:y_max]
            gradient_x = utils.frame_utils.warp_image_forward(gradient_x, roi_translation)
            gradient_y = utils.frame_utils.warp_image_forward(gradient_y, roi_translation)

        predicted_increment = roi_dense_flow[0] * gradient_x + roi_dense_flow[1] * gradient_y
        if self._gml_config["no_polarity"]:
            predicted_increment = torch.abs(predicted_increment)

        if weights is not None:
            predicted_increment *= weights
        predicted_increment /= torch.linalg.norm(predicted_increment.clone()) + 0.0001
        return predicted_increment

