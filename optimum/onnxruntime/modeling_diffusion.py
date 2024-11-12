#  Copyright 2023 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import importlib
import inspect
import logging
import os
import shutil
from abc import abstractmethod
from collections import OrderedDict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np
import onnxruntime as ort
import sympy as sp
import torch
from diffusers.configuration_utils import ConfigMixin
from diffusers.models.autoencoders.vae import DiagonalGaussianDistribution
from diffusers.pipelines import (
    AutoPipelineForImage2Image,
    AutoPipelineForInpainting,
    AutoPipelineForText2Image,
    LatentConsistencyModelImg2ImgPipeline,
    LatentConsistencyModelPipeline,
    StableDiffusionImg2ImgPipeline,
    StableDiffusionInpaintPipeline,
    StableDiffusionPipeline,
    StableDiffusionXLImg2ImgPipeline,
    StableDiffusionXLInpaintPipeline,
    StableDiffusionXLPipeline,
)
from diffusers.pipelines.pipeline_utils import DiffusionPipeline
from diffusers.schedulers import SchedulerMixin
from diffusers.schedulers.scheduling_utils import SCHEDULER_CONFIG_NAME
from diffusers.utils.constants import CONFIG_NAME
from huggingface_hub import snapshot_download
from huggingface_hub.constants import HUGGINGFACE_HUB_CACHE
from huggingface_hub.utils import validate_hf_hub_args
from transformers import CLIPFeatureExtractor, CLIPTokenizer
from transformers.file_utils import add_end_docstrings
from transformers.modeling_outputs import ModelOutput

from ..exporters.onnx import main_export
from ..onnx.utils import _get_model_external_data_paths
from ..utils import (
    DIFFUSION_MODEL_TEXT_ENCODER_2_SUBFOLDER,
    DIFFUSION_MODEL_TEXT_ENCODER_SUBFOLDER,
    DIFFUSION_MODEL_UNET_SUBFOLDER,
    DIFFUSION_MODEL_VAE_DECODER_SUBFOLDER,
    DIFFUSION_MODEL_VAE_ENCODER_SUBFOLDER,
)
from .io_binding import TypeHelper
from .modeling_ort import ONNX_MODEL_END_DOCSTRING, ORTModel
from .utils import (
    ONNX_WEIGHTS_NAME,
    get_device_for_provider,
    get_provider_for_device,
    np_to_pt_generators,
    parse_device,
    validate_provider_availability,
)


logger = logging.getLogger(__name__)


# TODO: support from_pipe()
# TODO: Instead of ORTModel, it makes sense to have a compositional ORTMixin
# TODO: instead of one bloated __init__, we should consider an __init__ per pipeline
class ORTDiffusionPipeline(ORTModel, DiffusionPipeline):
    config_name = "model_index.json"
    auto_model_class = DiffusionPipeline

    def __init__(
        self,
        scheduler: "SchedulerMixin",
        # optional pipeline models
        unet_session: Optional[ort.InferenceSession] = None,
        vae_decoder_session: Optional[ort.InferenceSession] = None,
        vae_encoder_session: Optional[ort.InferenceSession] = None,
        text_encoder_session: Optional[ort.InferenceSession] = None,
        text_encoder_2_session: Optional[ort.InferenceSession] = None,
        # optional pipeline submodels
        tokenizer: Optional["CLIPTokenizer"] = None,
        tokenizer_2: Optional["CLIPTokenizer"] = None,
        feature_extractor: Optional["CLIPFeatureExtractor"] = None,
        # stable diffusion xl specific arguments
        force_zeros_for_empty_prompt: bool = True,
        requires_aesthetics_score: bool = False,
        add_watermarker: Optional[bool] = None,
        # onnxruntime specific arguments
        use_io_binding: Optional[bool] = None,
        model_save_dir: Optional[Union[str, Path, TemporaryDirectory]] = None,
        **kwargs,
    ):
        if isinstance(model_save_dir, TemporaryDirectory):
            # This attribute is needed to keep one reference on the temporary directory, since garbage collecting it
            # would end-up removing the directory containing the underlying ONNX model.
            self._model_save_dir_tempdirectory_instance = model_save_dir
            self.model_save_dir = Path(model_save_dir.name)
        elif isinstance(model_save_dir, (str, Path)):
            self.model_save_dir = Path(model_save_dir)
        else:
            self.model_save_dir = Path(unet_session._model_path).parent

        # TODO: Maybe move this to from_pretrained so that the pipeline class can be instantiated with ORTModel instances
        self.unet = ORTModelUnet(unet_session, use_io_binding) if unet_session is not None else None
        self.vae_decoder = (
            ORTModelVaeDecoder(vae_decoder_session, use_io_binding) if vae_decoder_session is not None else None
        )
        self.vae_encoder = (
            ORTModelVaeEncoder(vae_encoder_session, use_io_binding) if vae_encoder_session is not None else None
        )
        self.text_encoder = (
            ORTModelTextEncoder(text_encoder_session, use_io_binding) if text_encoder_session is not None else None
        )
        self.text_encoder_2 = (
            ORTModelTextEncoder(text_encoder_2_session, use_io_binding) if text_encoder_2_session is not None else None
        )
        # We wrap the VAE Decoder & Encoder in a single object to simulate diffusers API
        self.vae = ORTWrapperVae(self.vae_encoder, self.vae_decoder)

        # we allow passing these as torch models for now
        self.image_encoder = kwargs.pop("image_encoder", None)  # TODO: maybe implement ORTModelImageEncoder
        self.safety_checker = kwargs.pop("safety_checker", None)  # TODO: maybe implement ORTModelSafetyChecker

        self.scheduler = scheduler
        self.tokenizer = tokenizer
        self.tokenizer_2 = tokenizer_2
        self.feature_extractor = feature_extractor

        all_pipeline_init_args = {
            "vae": self.vae,
            "unet": self.unet,
            "text_encoder": self.text_encoder,
            "text_encoder_2": self.text_encoder_2,
            "safety_checker": self.safety_checker,
            "image_encoder": self.image_encoder,
            "scheduler": self.scheduler,
            "tokenizer": self.tokenizer,
            "tokenizer_2": self.tokenizer_2,
            "feature_extractor": self.feature_extractor,
            "requires_aesthetics_score": requires_aesthetics_score,
            "force_zeros_for_empty_prompt": force_zeros_for_empty_prompt,
            "add_watermarker": add_watermarker,
        }

        diffusers_pipeline_args = {}
        for key in inspect.signature(self.auto_model_class).parameters.keys():
            if key in all_pipeline_init_args:
                diffusers_pipeline_args[key] = all_pipeline_init_args[key]
        self.auto_model_class.__init__(self, **diffusers_pipeline_args)

        # Forced on every class inheriting from OptimizedModel
        self.preprocessors = kwargs.pop("preprocessors", [])

    @property
    def components(self) -> Dict[str, Any]:
        components = {
            "vae": self.vae,
            "unet": self.unet,
            "text_encoder": self.text_encoder,
            "text_encoder_2": self.text_encoder_2,
            "safety_checker": self.safety_checker,
            "image_encoder": self.image_encoder,
        }
        components = {k: v for k, v in components.items() if v is not None}
        return components

    def _validate_same_attribute_value_across_components(self, attribute: str):
        # The idea is that these attributes make sense for the pipeline as a whole only when they are the same across
        # all components, so we do support these attributes but also allow the user to experiment with undefined behavior
        # like having heterogeneous devices or io bindings across components.
        attribute_values = {
            name: getattr(component, attribute)
            for name, component in self.components.items()
            if hasattr(component, attribute)
        }

        if len(attribute_values) == 0:
            raise ValueError(f"Attribute {attribute} is not defined for any component.")
        # make sure there is exactly one value for the attribute (bypass unhashable types)
        elif len(set(map(str, attribute_values.values()))) > 1:
            raise ValueError(f"Attribute {attribute} is not the same across components: {attribute_values}.")

        return next(iter(attribute_values.values()))

    @property
    def device(self) -> torch.device:
        return self._validate_same_attribute_value_across_components("device")

    @property
    def dtype(self) -> torch.dtype:
        return self._validate_same_attribute_value_across_components("dtype")

    @property
    def providers(self) -> Tuple[str]:
        return self._validate_same_attribute_value_across_components("providers")

    @property
    def provider(self) -> str:
        return self._validate_same_attribute_value_across_components("provider")

    @property
    def providers_options(self) -> Dict[str, Dict[str, Any]]:
        return self._validate_same_attribute_value_across_components("providers_options")

    @property
    def provider_options(self) -> Dict[str, Any]:
        return self._validate_same_attribute_value_across_components("provider_options")

    @property
    def use_io_binding(self) -> bool:
        return self._validate_same_attribute_value_across_components("use_io_binding")

    @use_io_binding.setter
    def use_io_binding(self, value):
        for component in self.components.values():
            if hasattr(component, "use_io_binding"):
                component.use_io_binding = value

    def to(self, *args, **kwargs):
        for component in self.components.values():
            component.to(*args, **kwargs)
        return self

    def __call__(self, *args, **kwargs):
        # we do this to keep numpy random states support for now
        # TODO: deprecate and add warnings when a random state is passed

        args = list(args)
        for i in range(len(args)):
            args[i] = np_to_pt_generators(args[i], self.device)

        for k, v in kwargs.items():
            kwargs[k] = np_to_pt_generators(v, self.device)

        return self.auto_model_class.__call__(self, *args, **kwargs)

    def _save_pretrained(self, save_directory: Union[str, Path]):
        save_directory = Path(save_directory)

        models_to_save_paths = {
            (self.unet, save_directory / DIFFUSION_MODEL_UNET_SUBFOLDER),
            (self.vae_decoder, save_directory / DIFFUSION_MODEL_VAE_DECODER_SUBFOLDER),
            (self.vae_encoder, save_directory / DIFFUSION_MODEL_VAE_ENCODER_SUBFOLDER),
            (self.text_encoder, save_directory / DIFFUSION_MODEL_TEXT_ENCODER_SUBFOLDER),
            (self.text_encoder_2, save_directory / DIFFUSION_MODEL_TEXT_ENCODER_2_SUBFOLDER),
        }
        for model, save_path in models_to_save_paths:
            if model is not None:
                model_path = Path(model.session._model_path)
                save_path.mkdir(parents=True, exist_ok=True)
                # copy onnx model
                shutil.copyfile(model_path, save_path / ONNX_WEIGHTS_NAME)
                # copy external onnx data
                external_data_paths = _get_model_external_data_paths(model_path)
                for external_data_path in external_data_paths:
                    shutil.copyfile(external_data_path, save_path / external_data_path.name)
                # copy model config
                config_path = model_path.parent / CONFIG_NAME
                if config_path.is_file():
                    config_save_path = save_path / CONFIG_NAME
                    shutil.copyfile(config_path, config_save_path)

        self.scheduler.save_pretrained(save_directory / "scheduler")

        if self.tokenizer is not None:
            self.tokenizer.save_pretrained(save_directory / "tokenizer")
        if self.tokenizer_2 is not None:
            self.tokenizer_2.save_pretrained(save_directory / "tokenizer_2")
        if self.feature_extractor is not None:
            self.feature_extractor.save_pretrained(save_directory / "feature_extractor")

    @classmethod
    def _from_pretrained(
        cls,
        model_id: Union[str, Path],
        config: Dict[str, Any],
        subfolder: str = "",
        force_download: bool = False,
        local_files_only: bool = False,
        revision: Optional[str] = None,
        trust_remote_code: bool = False,
        cache_dir: str = HUGGINGFACE_HUB_CACHE,
        token: Optional[Union[bool, str]] = None,
        unet_file_name: str = ONNX_WEIGHTS_NAME,
        vae_decoder_file_name: str = ONNX_WEIGHTS_NAME,
        vae_encoder_file_name: str = ONNX_WEIGHTS_NAME,
        text_encoder_file_name: str = ONNX_WEIGHTS_NAME,
        text_encoder_2_file_name: str = ONNX_WEIGHTS_NAME,
        use_io_binding: Optional[bool] = None,
        provider: str = "CPUExecutionProvider",
        provider_options: Optional[Dict[str, Any]] = None,
        session_options: Optional[ort.SessionOptions] = None,
        model_save_dir: Optional[Union[str, Path, TemporaryDirectory]] = None,
        **kwargs,
    ):
        if not os.path.isdir(str(model_id)):
            all_components = {key for key in config.keys() if not key.startswith("_")} | {"vae_encoder", "vae_decoder"}
            allow_patterns = {os.path.join(component, "*") for component in all_components}
            allow_patterns.update(
                {
                    unet_file_name,
                    vae_decoder_file_name,
                    vae_encoder_file_name,
                    text_encoder_file_name,
                    text_encoder_2_file_name,
                    SCHEDULER_CONFIG_NAME,
                    cls.config_name,
                    CONFIG_NAME,
                }
            )
            model_save_folder = snapshot_download(
                model_id,
                cache_dir=cache_dir,
                force_download=force_download,
                local_files_only=local_files_only,
                revision=revision,
                token=token,
                allow_patterns=allow_patterns,
                ignore_patterns=["*.msgpack", "*.safetensors", "*.bin", "*.xml"],
            )
        else:
            model_save_folder = str(model_id)

        model_save_path = Path(model_save_folder)

        if model_save_dir is None:
            model_save_dir = model_save_path

        model_paths = {
            "unet": model_save_path / DIFFUSION_MODEL_UNET_SUBFOLDER / unet_file_name,
            "vae_decoder": model_save_path / DIFFUSION_MODEL_VAE_DECODER_SUBFOLDER / vae_decoder_file_name,
            "vae_encoder": model_save_path / DIFFUSION_MODEL_VAE_ENCODER_SUBFOLDER / vae_encoder_file_name,
            "text_encoder": model_save_path / DIFFUSION_MODEL_TEXT_ENCODER_SUBFOLDER / text_encoder_file_name,
            "text_encoder_2": model_save_path / DIFFUSION_MODEL_TEXT_ENCODER_2_SUBFOLDER / text_encoder_2_file_name,
        }

        sessions = {}
        for model, path in model_paths.items():
            if kwargs.get(model, None) is not None:
                # this allows passing a model directly to from_pretrained
                sessions[f"{model}_session"] = kwargs.pop(model)
            else:
                sessions[f"{model}_session"] = (
                    ORTModel.load_model(path, provider, session_options, provider_options) if path.is_file() else None
                )

        submodels = {}
        for submodel in {"scheduler", "tokenizer", "tokenizer_2", "feature_extractor"}:
            if kwargs.get(submodel, None) is not None:
                submodels[submodel] = kwargs.pop(submodel)
            elif config.get(submodel, (None, None))[0] is not None:
                library_name, library_classes = config.get(submodel)
                library = importlib.import_module(library_name)
                class_obj = getattr(library, library_classes)
                load_method = getattr(class_obj, "from_pretrained")
                # Check if the module is in a subdirectory
                if (model_save_path / submodel).is_dir():
                    submodels[submodel] = load_method(model_save_path / submodel)
                else:
                    submodels[submodel] = load_method(model_save_path)

        # same as DiffusionPipeline.from_pretraoned, if called directly, it loads the class in the config
        if cls.__name__ == "ORTDiffusionPipeline":
            class_name = config["_class_name"]
            ort_pipeline_class = _get_ort_class(class_name)
        else:
            ort_pipeline_class = cls

        ort_pipeline = ort_pipeline_class(
            **sessions,
            **submodels,
            use_io_binding=use_io_binding,
            model_save_dir=model_save_dir,
            **kwargs,
        )

        # same as in DiffusionPipeline.from_pretrained, we save where the model was instantiated from
        ort_pipeline.register_to_config(_name_or_path=config.get("_name_or_path", str(model_id)))

        return ort_pipeline

    @classmethod
    def _export(
        cls,
        model_id: str,
        config: Dict[str, Any],
        subfolder: str = "",
        force_download: bool = False,
        local_files_only: bool = False,
        revision: Optional[str] = None,
        trust_remote_code: bool = False,
        cache_dir: str = HUGGINGFACE_HUB_CACHE,
        token: Optional[Union[bool, str]] = None,
        use_io_binding: Optional[bool] = None,
        provider: str = "CPUExecutionProvider",
        session_options: Optional[ort.SessionOptions] = None,
        provider_options: Optional[Dict[str, Any]] = None,
        task: Optional[str] = None,
        **kwargs,
    ) -> "ORTDiffusionPipeline":
        if task is None:
            task = cls._auto_model_to_task(cls.auto_model_class)

        # we continue passing the model_save_dir from here on to avoid it being cleaned up
        # might be better to use a persistent temporary directory such as the one implemented in
        # https://gist.github.com/twolfson/2929dc1163b0a76d2c2b66d51f9bc808
        model_save_dir = TemporaryDirectory()
        model_save_path = Path(model_save_dir.name)

        main_export(
            model_id,
            output=model_save_path,
            do_validation=False,
            no_post_process=True,
            token=token,
            revision=revision,
            cache_dir=cache_dir,
            subfolder=subfolder,
            force_download=force_download,
            local_files_only=local_files_only,
            trust_remote_code=trust_remote_code,
            library_name="diffusers",
            task=task,
        )

        return cls._from_pretrained(
            model_save_path,
            config=config,
            provider=provider,
            provider_options=provider_options,
            session_options=session_options,
            use_io_binding=use_io_binding,
            model_save_dir=model_save_dir,
            **kwargs,
        )

    @classmethod
    def _load_config(cls, config_name_or_path: Union[str, os.PathLike], **kwargs):
        return cls.load_config(config_name_or_path, **kwargs)

    def _save_config(self, save_directory):
        self.save_config(save_directory)


class ORTPipelinePart(ConfigMixin):
    config_name: str = CONFIG_NAME

    def __init__(self, session: ort.InferenceSession, use_io_binding: Optional[bool]):
        # config is mandatory for the model part to be used for inference
        config_file_path = Path(session._model_path).parent / self.config_name
        if not config_file_path.is_file():
            raise ValueError(f"Configuration file for {self.__class__.__name__} not found at {config_file_path}")
        else:
            self.register_to_config(**self._dict_from_json_file(config_file_path))

        self.session = session

        self._providers = self.session.get_providers()
        self._providers_options = self.session.get_provider_options()
        self._device = get_device_for_provider(provider=self.provider, provider_options=self.provider_options)

        self.input_names = {input_key.name: idx for idx, input_key in enumerate(self.session.get_inputs())}
        self.output_names = {output_key.name: idx for idx, output_key in enumerate(self.session.get_outputs())}
        self.input_dtypes = {input_key.name: input_key.type for input_key in self.session.get_inputs()}
        self.output_dtypes = {output_key.name: output_key.type for output_key in self.session.get_outputs()}
        self.input_shapes = {input_key.name: input_key.shape for input_key in self.session.get_inputs()}
        self.output_shapes = {output_key.name: output_key.shape for output_key in self.session.get_outputs()}

        # io binding stuff
        self.use_io_binding = (
            use_io_binding if use_io_binding is not None else self.provider in ["CUDAExecutionProvider"]
        )

        self._known_symbols = {k: v for k, v in self.config.items() if isinstance(v, int)}
        self._compiled_output_shapes = self._compile_shapes(self.output_shapes)
        self._compiled_input_shapes = self._compile_shapes(self.input_shapes)
        self._output_buffers = {}

    def _compile_shapes(self, shapes: Dict[str, Tuple[Union[int, str]]]) -> Dict[str, Tuple[sp.Basic]]:
        compiled_shapes = {}

        for key, shape in shapes.items():
            compiled_shapes[key] = tuple(sp.sympify(dim) for dim in shape)

        for key, shape in compiled_shapes.items():
            compiled_shapes[key] = tuple(dim.subs(self._known_symbols) for dim in shape)

        return compiled_shapes

    @property
    def device(self):
        return self._device

    @property
    def providers(self):
        return self._providers

    @property
    def provider(self):
        return self._providers[0]

    @property
    def providers_options(self):
        return self._providers_options

    @property
    def provider_options(self):
        return self._providers_options[self._providers[0]]

    @property
    def dtype(self):
        for dtype in self.input_dtypes.values():
            torch_dtype = TypeHelper.ort_type_to_torch_type(dtype)
            if torch_dtype.is_floating_point:
                return torch_dtype

        for dtype in self.output_dtypes.values():
            torch_dtype = TypeHelper.ort_type_to_torch_type(dtype)
            if torch_dtype.is_floating_point:
                return torch_dtype

        return None

    def to(self, *args, device: Optional[Union[int, str, torch.device]] = None, dtype: Optional[torch.dtype] = None):
        for arg in args:
            if isinstance(arg, (int, str, torch.device)):
                device = torch.device(arg)
            elif isinstance(arg, torch.dtype):
                dtype = arg

        if dtype is not None and dtype != self.dtype:
            raise NotImplementedError(
                f"Cannot change the dtype of the pipeline from {self.dtype} to {dtype}. "
                f"Please export the pipeline with the desired dtype."
            )

        if device is None or device == self.device:
            return self

        device, provider_options = parse_device(device)
        provider = get_provider_for_device(device)
        validate_provider_availability(provider)

        self.session.set_providers([provider], provider_options=[provider_options])

        self._providers = self.session.get_providers()
        self._providers_options = self.session.get_provider_options()

        if self.provider != provider or self.provider_options != provider_options:
            raise ValueError(
                f"Failed to set the device to {device}. "
                f"Requested provider {provider} with options: {provider_options}, "
                f"but got provider {self.provider} with options: {self.provider_options}."
            )

        self._device = device

        return self

    def _get_output_shapes(self, **model_inputs: torch.Tensor) -> Dict[str, Tuple[int, ...]]:
        known_symbols = self._known_symbols.copy()

        for input_name, compiled_input_shape in self._compiled_input_shapes.items():
            input_tensor_shape = model_inputs[input_name].shape
            for expr, dim in zip(compiled_input_shape, input_tensor_shape):
                if len(expr.free_symbols) == 0:
                    continue
                elif len(expr.free_symbols) == 1:
                    symbol = expr.free_symbols.pop()
                    if expr == symbol:
                        known_symbols[symbol] = dim
                    else:
                        resolved_symbol = sp.solve(expr - dim, symbol)
                        if resolved_symbol:
                            known_symbols[symbol] = int(resolved_symbol[0])
                        else:
                            raise ValueError(
                                f"Failed to resolve the symbolic dimension {symbol} for the input {input_name}. "
                                f"Expression: {expr}, Dimension: {dim}"
                            )
                else:
                    raise ValueError(
                        f"Symbolic dimension for the input {input_name} has more than one free symbol. "
                        f"Expression: {expr}, Dimension: {dim}"
                    )

        resolved_output_shapes = {}
        unresolved_output_shapes = {}

        for output_name, compiled_output_shape in self._compiled_output_shapes.items():
            shape = tuple(dim.subs(known_symbols) for dim in compiled_output_shape)

            if any(dim.free_symbols for dim in shape):
                unresolved_output_shapes[output_name] = shape
            else:
                resolved_output_shapes[output_name] = shape

        if unresolved_output_shapes:
            raise ValueError(
                f"Failed to resolve the symbolic dimensions for the model {self.__class__.__name__}. "
                f"Unresolved output shapes: {unresolved_output_shapes}"
            )

        return resolved_output_shapes

    def _prepare_io_binding(self, model_inputs: torch.Tensor, output_shapes:Dict[str, Tuple[int, ...]] | None = None) -> Tuple[ort.IOBinding, Dict[str, torch.Tensor]]:
        io_binding = self.session.io_binding()

        for input_name in self.input_names.keys():
            input_dtype = TypeHelper.ort_type_to_torch_type(self.input_dtypes[input_name])

            if model_inputs[input_name].dtype != input_dtype:
                model_inputs[input_name] = model_inputs[input_name].to(input_dtype)

            io_binding.bind_input(
                name=input_name,
                device_type=self.device.type,
                device_id=self.device.index if self.device.index is not None else -1,
                element_type=TypeHelper.ort_type_to_numpy_type(self.input_dtypes[input_name]),
                buffer_ptr=model_inputs[input_name].data_ptr(),
                shape=tuple(model_inputs[input_name].size()),
            )

        if output_shapes is None:   
            current_output_shapes = self._get_output_shapes(**model_inputs)
        else:
            current_output_shapes = output_shapes

        for output_name in self.output_names.keys():
            output_dtype = TypeHelper.ort_type_to_torch_type(self.output_dtypes[output_name])
            output_shape = current_output_shapes[output_name]

            if output_name not in self._output_buffers or self._output_buffers[output_name].shape != output_shape:
                self._output_buffers[output_name] = torch.empty(output_shape, device=self.device, dtype=output_dtype)

            io_binding.bind_output(
                name=output_name,
                device_type=self.device.type,
                device_id=self.device.index if self.device.index is not None else -1,
                element_type=TypeHelper.ort_type_to_numpy_type(self.output_dtypes[output_name]),
                buffer_ptr=self._output_buffers[output_name].data_ptr(),
                shape=tuple(self._output_buffers[output_name].size()),
            )

        return io_binding, self._output_buffers

    def _prepare_onnx_inputs(self, **inputs: Union[torch.Tensor, np.ndarray]) -> Dict[str, np.ndarray]:
        onnx_inputs = {}

        for input_name in self.input_names.keys():
            onnx_inputs[input_name] = inputs.pop(input_name)

            if isinstance(onnx_inputs[input_name], torch.Tensor):
                onnx_inputs[input_name] = onnx_inputs[input_name].numpy(force=True)

            if onnx_inputs[input_name].dtype != self.input_dtypes[input_name]:
                onnx_inputs[input_name] = onnx_inputs[input_name].astype(
                    TypeHelper.ort_type_to_numpy_type(self.input_dtypes[input_name])
                )

        return onnx_inputs

    def _prepare_onnx_outputs(self, *onnx_outputs: np.ndarray) -> Dict[str, Union[torch.Tensor, np.ndarray]]:
        model_outputs = {}

        for output_name, idx in self.output_names.items():
            model_outputs[output_name] = onnx_outputs[idx]

            if isinstance(model_outputs[output_name], np.ndarray):
                model_outputs[output_name] = torch.from_numpy(model_outputs[output_name]).to(self.device)

        return model_outputs

    @abstractmethod
    def forward(self, *args, **kwargs):
        raise NotImplementedError

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)


class ORTModelUnet(ORTPipelinePart):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # can be missing from models exported long ago
        if not hasattr(self.config, "time_cond_proj_dim"):
            logger.warning(
                "The `time_cond_proj_dim` attribute is missing from the UNet configuration. "
                "Please re-export the model with newer version of optimum and diffusers."
            )
            self.register_to_config(time_cond_proj_dim=None)

    def forward(
        self,
        sample: Union[np.ndarray, torch.Tensor],
        timestep: Union[np.ndarray, torch.Tensor],
        encoder_hidden_states: Union[np.ndarray, torch.Tensor],
        text_embeds: Optional[Union[np.ndarray, torch.Tensor]] = None,
        time_ids: Optional[Union[np.ndarray, torch.Tensor]] = None,
        timestep_cond: Optional[Union[np.ndarray, torch.Tensor]] = None,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        added_cond_kwargs: Optional[Dict[str, Any]] = None,
        return_dict: bool = False,
    ):
        if len(timestep.shape) == 0:
            timestep = timestep.unsqueeze(0)

        model_inputs = {
            "sample": sample,
            "timestep": timestep,
            "encoder_hidden_states": encoder_hidden_states,
            "text_embeds": text_embeds,
            "time_ids": time_ids,
            "timestep_cond": timestep_cond,
            **(cross_attention_kwargs or {}),
            **(added_cond_kwargs or {}),
        }

        if self.use_io_binding:
            # _get_output_shapes is very slow. Here we compute the output shape quickly.
            output_shape = list(sample.size())
            output_shape[1] = 4
            outputs_shape = {next(iter(self.output_names)) : tuple(output_shape)}
            io_binding, model_outputs = self._prepare_io_binding(model_inputs, outputs_shape)
                
            self.session.run_with_iobinding(io_binding)
        else:
            onnx_inputs = self._prepare_onnx_inputs(**model_inputs)

            onnx_outputs = self.session.run(None, onnx_inputs)

            model_outputs = self._prepare_onnx_outputs(*onnx_outputs)

        if return_dict:
            return model_outputs

        return ModelOutput(**model_outputs)


class ORTModelTextEncoder(ORTPipelinePart):
    def forward(
        self,
        input_ids: Union[np.ndarray, torch.Tensor],
        attention_mask: Optional[Union[np.ndarray, torch.Tensor]] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: bool = False,
    ):
        model_inputs = {"input_ids": input_ids}

        if self.use_io_binding:
            io_binding, model_outputs = self._prepare_io_binding(model_inputs)
            self.session.run_with_iobinding(io_binding)
        else:
            onnx_inputs = self._prepare_onnx_inputs(**model_inputs)
            onnx_outputs = self.session.run(None, onnx_inputs)
            model_outputs = self._prepare_onnx_outputs(*onnx_outputs)

        if output_hidden_states:
            model_outputs["hidden_states"] = []
            for i in range(self.config.num_hidden_layers):
                model_outputs["hidden_states"].append(model_outputs.pop(f"hidden_states.{i}"))
            model_outputs["hidden_states"].append(model_outputs.get("last_hidden_state"))
        else:
            for i in range(self.config.num_hidden_layers):
                model_outputs.pop(f"hidden_states.{i}", None)

        if return_dict:
            return model_outputs

        return ModelOutput(**model_outputs)


class ORTModelVaeEncoder(ORTPipelinePart):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # can be missing from models exported long ago
        if not hasattr(self.config, "scaling_factor"):
            logger.warning(
                "The `scaling_factor` attribute is missing from the VAE encoder configuration. "
                "Please re-export the model with newer version of optimum and diffusers."
            )
            self.register_to_config(scaling_factor=0.18215)

    def forward(
        self,
        sample: Union[np.ndarray, torch.Tensor],
        generator: Optional[torch.Generator] = None,
        return_dict: bool = False,
    ):
        model_inputs = {"sample": sample}

        if self.use_io_binding:
            io_binding, model_outputs = self._prepare_io_binding(model_inputs)
            self.session.run_with_iobinding(io_binding)
        else:
            onnx_inputs = self._prepare_onnx_inputs(**model_inputs)
            onnx_outputs = self.session.run(None, onnx_inputs)
            model_outputs = self._prepare_onnx_outputs(*onnx_outputs)

        if "latent_sample" in model_outputs:
            model_outputs["latents"] = model_outputs.pop("latent_sample")

        if "latent_parameters" in model_outputs:
            model_outputs["latent_dist"] = DiagonalGaussianDistribution(
                parameters=model_outputs.pop("latent_parameters")
            )

        if return_dict:
            return model_outputs

        return ModelOutput(**model_outputs)


class ORTModelVaeDecoder(ORTPipelinePart):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # can be missing from models exported long ago
        if not hasattr(self.config, "scaling_factor"):
            logger.warning(
                "The `scaling_factor` attribute is missing from the VAE encoder configuration. "
                "Please re-export the model with newer version of optimum and diffusers."
            )
            self.register_to_config(scaling_factor=0.18215)

    def forward(
        self,
        latent_sample: Union[np.ndarray, torch.Tensor],
        generator: Optional[torch.Generator] = None,
        return_dict: bool = False,
    ):
        model_inputs = {"latent_sample": latent_sample}

        if self.use_io_binding:
            io_binding, model_outputs = self._prepare_io_binding(model_inputs)
            self.session.run_with_iobinding(io_binding)
        else:
            onnx_inputs = self._prepare_onnx_inputs(**model_inputs)
            onnx_outputs = self.session.run(None, onnx_inputs)
            model_outputs = self._prepare_onnx_outputs(*onnx_outputs)

        if "latent_sample" in model_outputs:
            model_outputs["latents"] = model_outputs.pop("latent_sample")

        if return_dict:
            return model_outputs

        return ModelOutput(**model_outputs)


class ORTWrapperVae(ORTPipelinePart):
    def __init__(self, encoder: ORTModelVaeEncoder, decoder: ORTModelVaeDecoder):
        self.decoder = decoder
        self.encoder = encoder

    @property
    def config(self):
        return self.decoder.config

    @property
    def dtype(self):
        return self.decoder.dtype

    @property
    def device(self):
        return self.decoder.device

    def decode(self, *args, **kwargs):
        return self.decoder(*args, **kwargs)

    def encode(self, *args, **kwargs):
        return self.encoder(*args, **kwargs)

    def to(self, *args, **kwargs):
        self.decoder.to(*args, **kwargs)
        if self.encoder is not None:
            self.encoder.to(*args, **kwargs)


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTStableDiffusionPipeline(ORTDiffusionPipeline, StableDiffusionPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.StableDiffusionPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/text2img#diffusers.StableDiffusionPipeline).
    """

    main_input_name = "prompt"
    export_feature = "text-to-image"
    auto_model_class = StableDiffusionPipeline


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTStableDiffusionImg2ImgPipeline(ORTDiffusionPipeline, StableDiffusionImg2ImgPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.StableDiffusionImg2ImgPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/img2img#diffusers.StableDiffusionImg2ImgPipeline).
    """

    main_input_name = "image"
    export_feature = "image-to-image"
    auto_model_class = StableDiffusionImg2ImgPipeline


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTStableDiffusionInpaintPipeline(ORTDiffusionPipeline, StableDiffusionInpaintPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.StableDiffusionInpaintPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/inpaint#diffusers.StableDiffusionInpaintPipeline).
    """

    main_input_name = "prompt"
    export_feature = "inpainting"
    auto_model_class = StableDiffusionInpaintPipeline


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTStableDiffusionXLPipeline(ORTDiffusionPipeline, StableDiffusionXLPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.StableDiffusionXLPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/stable_diffusion_xl#diffusers.StableDiffusionXLPipeline).
    """

    main_input_name = "prompt"
    export_feature = "text-to-image"
    auto_model_class = StableDiffusionXLPipeline

    def _get_add_time_ids(
        self,
        original_size,
        crops_coords_top_left,
        target_size,
        dtype,
        text_encoder_projection_dim=None,
    ):
        add_time_ids = list(original_size + crops_coords_top_left + target_size)

        add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
        return add_time_ids


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTStableDiffusionXLImg2ImgPipeline(ORTDiffusionPipeline, StableDiffusionXLImg2ImgPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.StableDiffusionXLImg2ImgPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/stable_diffusion_xl#diffusers.StableDiffusionXLImg2ImgPipeline).
    """

    main_input_name = "prompt"
    export_feature = "image-to-image"
    auto_model_class = StableDiffusionXLImg2ImgPipeline

    def _get_add_time_ids(
        self,
        original_size,
        crops_coords_top_left,
        target_size,
        aesthetic_score,
        negative_aesthetic_score,
        negative_original_size,
        negative_crops_coords_top_left,
        negative_target_size,
        dtype,
        text_encoder_projection_dim=None,
    ):
        if self.config.requires_aesthetics_score:
            add_time_ids = list(original_size + crops_coords_top_left + (aesthetic_score,))
            add_neg_time_ids = list(
                negative_original_size + negative_crops_coords_top_left + (negative_aesthetic_score,)
            )
        else:
            add_time_ids = list(original_size + crops_coords_top_left + target_size)
            add_neg_time_ids = list(negative_original_size + crops_coords_top_left + negative_target_size)

        add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
        add_neg_time_ids = torch.tensor([add_neg_time_ids], dtype=dtype)

        return add_time_ids, add_neg_time_ids


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTStableDiffusionXLInpaintPipeline(ORTDiffusionPipeline, StableDiffusionXLInpaintPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.StableDiffusionXLInpaintPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/stable_diffusion_xl#diffusers.StableDiffusionXLInpaintPipeline).
    """

    main_input_name = "image"
    export_feature = "inpainting"
    auto_model_class = StableDiffusionXLInpaintPipeline

    def _get_add_time_ids(
        self,
        original_size,
        crops_coords_top_left,
        target_size,
        aesthetic_score,
        negative_aesthetic_score,
        negative_original_size,
        negative_crops_coords_top_left,
        negative_target_size,
        dtype,
        text_encoder_projection_dim=None,
    ):
        if self.config.requires_aesthetics_score:
            add_time_ids = list(original_size + crops_coords_top_left + (aesthetic_score,))
            add_neg_time_ids = list(
                negative_original_size + negative_crops_coords_top_left + (negative_aesthetic_score,)
            )
        else:
            add_time_ids = list(original_size + crops_coords_top_left + target_size)
            add_neg_time_ids = list(negative_original_size + crops_coords_top_left + negative_target_size)

        add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
        add_neg_time_ids = torch.tensor([add_neg_time_ids], dtype=dtype)

        return add_time_ids, add_neg_time_ids


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTLatentConsistencyModelPipeline(ORTDiffusionPipeline, LatentConsistencyModelPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.LatentConsistencyModelPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/latent_consistency#diffusers.LatentConsistencyModelPipeline).
    """

    main_input_name = "prompt"
    export_feature = "text-to-image"
    auto_model_class = LatentConsistencyModelPipeline


@add_end_docstrings(ONNX_MODEL_END_DOCSTRING)
class ORTLatentConsistencyModelImg2ImgPipeline(ORTDiffusionPipeline, LatentConsistencyModelImg2ImgPipeline):
    """
    ONNX Runtime-powered stable diffusion pipeline corresponding to [diffusers.LatentConsistencyModelImg2ImgPipeline](https://huggingface.co/docs/diffusers/api/pipelines/stable_diffusion/latent_consistency_img2img#diffusers.LatentConsistencyModelImg2ImgPipeline).
    """

    main_input_name = "image"
    export_feature = "image-to-image"
    auto_model_class = LatentConsistencyModelImg2ImgPipeline


SUPPORTED_ORT_PIPELINES = [
    ORTStableDiffusionPipeline,
    ORTStableDiffusionImg2ImgPipeline,
    ORTStableDiffusionInpaintPipeline,
    ORTStableDiffusionXLPipeline,
    ORTStableDiffusionXLImg2ImgPipeline,
    ORTStableDiffusionXLInpaintPipeline,
    ORTLatentConsistencyModelPipeline,
    ORTLatentConsistencyModelImg2ImgPipeline,
]


def _get_ort_class(pipeline_class_name: str, throw_error_if_not_exist: bool = True):
    for ort_pipeline_class in SUPPORTED_ORT_PIPELINES:
        if (
            ort_pipeline_class.__name__ == pipeline_class_name
            or ort_pipeline_class.auto_model_class.__name__ == pipeline_class_name
        ):
            return ort_pipeline_class

    if throw_error_if_not_exist:
        raise ValueError(f"ORTDiffusionPipeline can't find a pipeline linked to {pipeline_class_name}")


ORT_TEXT2IMAGE_PIPELINES_MAPPING = OrderedDict(
    [
        ("stable-diffusion", ORTStableDiffusionPipeline),
        ("stable-diffusion-xl", ORTStableDiffusionXLPipeline),
        ("latent-consistency", ORTLatentConsistencyModelPipeline),
    ]
)

ORT_IMAGE2IMAGE_PIPELINES_MAPPING = OrderedDict(
    [
        ("stable-diffusion", ORTStableDiffusionImg2ImgPipeline),
        ("stable-diffusion-xl", ORTStableDiffusionXLImg2ImgPipeline),
        ("latent-consistency", ORTLatentConsistencyModelImg2ImgPipeline),
    ]
)

ORT_INPAINT_PIPELINES_MAPPING = OrderedDict(
    [
        ("stable-diffusion", ORTStableDiffusionInpaintPipeline),
        ("stable-diffusion-xl", ORTStableDiffusionXLInpaintPipeline),
    ]
)

SUPPORTED_ORT_PIPELINES_MAPPINGS = [
    ORT_TEXT2IMAGE_PIPELINES_MAPPING,
    ORT_IMAGE2IMAGE_PIPELINES_MAPPING,
    ORT_INPAINT_PIPELINES_MAPPING,
]


def _get_task_ort_class(mapping, pipeline_class_name):
    def _get_model_name(pipeline_class_name):
        for ort_pipelines_mapping in SUPPORTED_ORT_PIPELINES_MAPPINGS:
            for model_name, ort_pipeline_class in ort_pipelines_mapping.items():
                if (
                    ort_pipeline_class.__name__ == pipeline_class_name
                    or ort_pipeline_class.auto_model_class.__name__ == pipeline_class_name
                ):
                    return model_name

    model_name = _get_model_name(pipeline_class_name)

    if model_name is not None:
        task_class = mapping.get(model_name, None)
        if task_class is not None:
            return task_class

    raise ValueError(f"ORTPipelineForTask can't find a pipeline linked to {pipeline_class_name} for {model_name}")


class ORTPipelineForTask(ConfigMixin):
    config_name = "model_index.json"

    @classmethod
    @validate_hf_hub_args
    def from_pretrained(cls, pretrained_model_or_path, **kwargs) -> ORTDiffusionPipeline:
        load_config_kwargs = {
            "force_download": kwargs.get("force_download", False),
            "resume_download": kwargs.get("resume_download", None),
            "local_files_only": kwargs.get("local_files_only", False),
            "cache_dir": kwargs.get("cache_dir", None),
            "revision": kwargs.get("revision", None),
            "proxies": kwargs.get("proxies", None),
            "token": kwargs.get("token", None),
        }
        config = cls.load_config(pretrained_model_or_path, **load_config_kwargs)
        config = config[0] if isinstance(config, tuple) else config
        class_name = config["_class_name"]

        ort_pipeline_class = _get_task_ort_class(cls.ort_pipelines_mapping, class_name)

        return ort_pipeline_class.from_pretrained(pretrained_model_or_path, **kwargs)


class ORTPipelineForText2Image(ORTPipelineForTask):
    auto_model_class = AutoPipelineForText2Image
    ort_pipelines_mapping = ORT_TEXT2IMAGE_PIPELINES_MAPPING


class ORTPipelineForImage2Image(ORTPipelineForTask):
    auto_model_class = AutoPipelineForImage2Image
    ort_pipelines_mapping = ORT_IMAGE2IMAGE_PIPELINES_MAPPING


class ORTPipelineForInpainting(ORTPipelineForTask):
    auto_model_class = AutoPipelineForInpainting
    ort_pipelines_mapping = ORT_INPAINT_PIPELINES_MAPPING
