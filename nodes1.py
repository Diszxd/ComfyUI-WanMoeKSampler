import torch
import comfy.sample
import comfy.samplers
import comfy.utils
import comfy.model_sampling
import latent_preview

def get_or_create_model_sampling(model):
    model_sampling = model.get_model_object("model_sampling")
    if model_sampling is not None:
        return model_sampling

    # Create a new model_sampling object if not found
    sampling_base = comfy.model_sampling.ModelSamplingDiscreteFlow
    sampling_type = comfy.model_sampling.CONST

    class ModelSamplingAdvanced(sampling_base, sampling_type):
        pass

    model_config = model.model.model_config
    model_sampling = ModelSamplingAdvanced(model_config)
    return model_sampling

def set_shift(model, sigma_shift):
    model_sampling = get_or_create_model_sampling(model)
    model_sampling.set_parameters(shift=sigma_shift, multiplier=1000)
    model.add_object_patch("model_sampling", model_sampling)
    return model

def wan_ksampler(model_high_noise, model_low_noise, seed, steps, cfgs, sampler_name, scheduler, positive, negative, latent, boundary=0.875, denoise=1.0, disable_noise=False, start_step=None, last_step=None, force_full_denoise=False):
    latent_image = latent["samples"]

    if disable_noise:
        noise = torch.zeros(latent_image.size(), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
    else:
        batch_inds = latent["batch_index"] if "batch_index" in latent else None
        noise = comfy.sample.prepare_noise(latent_image, seed, batch_inds)

    noise_mask = None
    if "noise_mask" in latent:
        noise_mask = latent["noise_mask"]

    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    assert start_step is None or start_step < steps
    assert last_step is None or last_step >= start_step
    if start_step is None:
        start_step = 0
    if last_step is None:
        last_step=9999

    # Get model_sampling safely
    sampling = get_or_create_model_sampling(model_high_noise)
    sigmas = comfy.samplers.calculate_sigmas(sampling, scheduler, steps)
    
    # Convert sigmas to timesteps (0-1 range)
    timesteps = [sampling.timestep(sigma)/1000 for sigma in sigmas.tolist()]
    
    switching_step = steps
    for i, t in enumerate(timesteps[1:]):
        if t < boundary:
            switching_step = i
            break
            
    print(f"switching model at step {switching_step}")
    start_with_high = start_step < switching_step
    end_wth_low = last_step >= switching_step

    if start_with_high:
        print("Running high noise model...")
        callback = latent_preview.prepare_callback(model_high_noise, steps)
        end_step = min(last_step, switching_step)
        latent_image = comfy.sample.fix_empty_latent_channels(model_high_noise, latent_image)
        latent_image = comfy.sample.sample(
            model_high_noise, noise, steps, cfgs[0], sampler_name, scheduler, 
            positive, negative, latent_image, denoise=denoise, 
            disable_noise=end_wth_low or disable_noise, start_step=start_step, 
            last_step=end_step, force_full_denoise=end_wth_low or force_full_denoise, 
            noise_mask=noise_mask, callback=callback, disable_pbar=disable_pbar, seed=seed
        )

    if end_wth_low:
        print("Running low noise model...")
        callback = latent_preview.prepare_callback(model_low_noise, steps)
        begin_step = max(start_step, switching_step)
        latent_image = comfy.sample.fix_empty_latent_channels(model_low_noise, latent_image)
        latent_image = comfy.sample.sample(
            model_low_noise, noise, steps, cfgs[1], sampler_name, scheduler, 
            positive, negative, latent_image, denoise=denoise, 
            disable_noise=disable_noise, start_step=begin_step, last_step=last_step,
            force_full_denoise=force_full_denoise, noise_mask=noise_mask, 
            callback=callback, disable_pbar=disable_pbar, seed=seed
        )

    out = latent.copy()
    out["samples"] = latent_image
    return (out, )

class WanMoeKSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model_high_noise": ("MODEL", {"tooltip": "The first expert of the model used for denoising the input latent."}),
                "model_low_noise": ("MODEL", {"tooltip": "The second expert of the model used for denoising the input latent."}),
                "boundary": ("FLOAT", {"default": 0.875, "min": 0.0, "max": 1.0, "step": 0.001, "round": 0.001, "tooltip": "Boundary (or t_moe): Timestep (not to be confused with denoising step) at which models should be switched. Recommended values: 0.875 for t2v, 0.9 for i2v"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True, "tooltip": "The random seed used for creating the noise."}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 10000, "tooltip": "The number of steps used in the denoising process."}),
                "cfg_high_noise": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01, "tooltip": "The Classifier-Free Guidance scale for high noise model."}),
                "cfg_low_noise": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01, "tooltip": "The Classifier-Free Guidance scale for low noise model."}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"tooltip": "The algorithm used when sampling, this can affect the quality, speed, and style of the generated output."}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"tooltip": "The scheduler controls how noise is gradually removed to form the image."}),
                "sigma_shift_high": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.01, "tooltip": "Shift parameter for high noise model (similar to ModelSamplingSD3)"}),
                "sigma_shift_low": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.01, "tooltip": "Shift parameter for low noise model (similar to ModelSamplingSD3)"}),
                "positive": ("CONDITIONING", {"tooltip": "The conditioning describing the attributes you want to include in the image."}),
                "negative": ("CONDITIONING", {"tooltip": "The conditioning describing the attributes you want to exclude from the image."}),
                "latent_image": ("LATENT", {"tooltip": "The latent image to denoise."}),
                "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "The amount of denoising applied, lower values will maintain the structure of the initial image allowing for image to image sampling."}),
            }
        }

    RETURN_TYPES = ("LATENT",)
    OUTPUT_TOOLTIPS = ("The denoised latent.",)
    FUNCTION = "sample"

    CATEGORY = "sampling"
    DESCRIPTION = "Uses the provided model, positive and negative conditioning to denoise the latent image."

    def sample(self, model_high_noise, model_low_noise, boundary, seed, steps, cfg_high_noise, cfg_low_noise, sampler_name, scheduler, sigma_shift_high, sigma_shift_low, positive, negative, latent_image, denoise=1.0):
        model_high_noise = set_shift(model_high_noise, sigma_shift_high)
        model_low_noise = set_shift(model_low_noise, sigma_shift_low)

        return wan_ksampler(
            model_high_noise, 
            model_low_noise, 
            seed, 
            steps, 
            (cfg_high_noise, cfg_low_noise), 
            sampler_name, 
            scheduler, 
            positive, 
            negative, 
            latent_image, 
            boundary=boundary, 
            denoise=denoise
        )

class WanMoeKSamplerAdvanced:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                "model_high_noise": ("MODEL", {"tooltip": "The first expert of the model used for denoising the input latent."}),
                "model_low_noise": ("MODEL", {"tooltip": "The second expert of the model used for denoising the input latent."}),
                "boundary": ("FLOAT", {"default": 0.875, "min": 0.0, "max": 1.0, "step": 0.001, "round": 0.001}),
                "add_noise": (["enable", "disable"], ),
                "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                "cfg_high_noise": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01}),
                "cfg_low_noise": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                "sigma_shift_high": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.01, "tooltip": "Shift parameter for high noise model"}),
                "sigma_shift_low": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0, "step": 0.01, "tooltip": "Shift parameter for low noise model"}),
                "positive": ("CONDITIONING", ),
                "negative": ("CONDITIONING", ),
                "latent_image": ("LATENT", ),
                "start_at_step": ("INT", {"default": 0, "min": 0, "max": 10000}),
                "end_at_step": ("INT", {"default": 10000, "min": 0, "max": 10000}),
                "return_with_leftover_noise": (["disable", "enable"], ),
            }
        }

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "sample"

    CATEGORY = "sampling"

    def sample(self, model_high_noise, model_low_noise, boundary, add_noise, noise_seed, steps, cfg_high_noise, cfg_low_noise, sampler_name, scheduler, sigma_shift_high, sigma_shift_low, positive, negative, latent_image, start_at_step, end_at_step, return_with_leftover_noise, denoise=1.0):
        model_high_noise = set_shift(model_high_noise, sigma_shift_high)
        model_low_noise = set_shift(model_low_noise, sigma_shift_low)
        
        force_full_denoise = True
        if return_with_leftover_noise == "enable":
            force_full_denoise = False
            
        disable_noise = False
        if add_noise == "disable":
            disable_noise = True
            
        return wan_ksampler(
            model_high_noise, 
            model_low_noise, 
            noise_seed, 
            steps, 
            (cfg_high_noise, cfg_low_noise), 
            sampler_name, 
            scheduler, 
            positive, 
            negative, 
            latent_image, 
            boundary=boundary, 
            denoise=denoise, 
            disable_noise=disable_noise, 
            start_step=start_at_step, 
            last_step=end_at_step, 
            force_full_denoise=force_full_denoise
        )

class SplitSigmasAtT:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {
                        "boundary": ("FLOAT", {"default": 0.875, "min": 0.0, "max": 1.0, "step": 0.001, "round":0.001}),
                        "sigmas": ("SIGMAS", ),
                    },
                 "optional":                     
                    {
                        "model": ("MODEL", {"tooltip": "Used to determine the model type. Assumes FLOW model by default if not provided"}),
                    }
                }
    
    RETURN_TYPES = ("SIGMAS", "SIGMAS", "INT", )
    RETURN_NAMES = ("high noise sigmas", "low noise sigmas", "split at", )
    CATEGORY = "sampling/custom_sampling/schedulers"

    FUNCTION = "split"

    def split(self, boundary, sigmas:torch.Tensor, model = None):
        if model is None:
            # Create default sampling object
            sampling_base = comfy.model_sampling.ModelSamplingDiscreteFlow  
            sampling_type = comfy.model_sampling.CONST

            class ModelSamplingAdvanced(sampling_base, sampling_type):
                pass
                
            sampling = ModelSamplingAdvanced()
        else:
            sampling = get_or_create_model_sampling(model)

        timesteps = [sampling.timestep(sigma)/1000 for sigma in sigmas.tolist()]
        switching_step = sigmas.size(0)
        for i, t in enumerate(timesteps[1:]):
            if t < boundary:
                switching_step = i
                break
                
        print(f"splitting sigmas at index {switching_step}")
        return (sigmas[:switching_step + 1], sigmas[switching_step:], switching_step, )