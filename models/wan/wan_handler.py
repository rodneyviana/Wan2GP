import torch
import numpy as np

def test_class_i2v(base_model_type):    
    return base_model_type in ["i2v", "i2v_2_2", "fun_inp_1.3B", "fun_inp", "flf2v_720p",  "fantasy",  "multitalk", "i2v_2_2_multitalk" ] #"hunyuan_i2v",

def test_class_1_3B(base_model_type):    
    return base_model_type in [ "vace_1.3B", "t2v_1.3B", "recam_1.3B","phantom_1.3B","fun_inp_1.3B"]

class family_handler():

    @staticmethod
    def set_cache_parameters(cache_type, base_model_type, model_def, inputs, skip_steps_cache):
        i2v =  test_class_i2v(base_model_type)

        resolution = inputs["resolution"]
        width, height = resolution.split("x")
        pixels = int(width) * int(height)

        if cache_type == "mag":
            skip_steps_cache.update({     
            "magcache_thresh" : 0,
            "magcache_K" : 2,
            })
            if base_model_type in ["t2v"] and "URLs2" in model_def:
                def_mag_ratios = [1.00124, 1.00155, 0.99822, 0.99851, 0.99696, 0.99687, 0.99703, 0.99732, 0.9966, 0.99679, 0.99602, 0.99658, 0.99578, 0.99664, 0.99484, 0.9949, 0.99633, 0.996, 0.99659, 0.99683, 0.99534, 0.99549, 0.99584, 0.99577, 0.99681, 0.99694, 0.99563, 0.99554, 0.9944, 0.99473, 0.99594, 0.9964, 0.99466, 0.99461, 0.99453, 0.99481, 0.99389, 0.99365, 0.99391, 0.99406, 0.99354, 0.99361, 0.99283, 0.99278, 0.99268, 0.99263, 0.99057, 0.99091, 0.99125, 0.99126, 0.65523, 0.65252, 0.98808, 0.98852, 0.98765, 0.98736, 0.9851, 0.98535, 0.98311, 0.98339, 0.9805, 0.9806, 0.97776, 0.97771, 0.97278, 0.97286, 0.96731, 0.96728, 0.95857, 0.95855, 0.94385, 0.94385, 0.92118, 0.921, 0.88108, 0.88076, 0.80263, 0.80181]
            elif base_model_type in ["i2v_2_2"]:
                def_mag_ratios = [0.99191, 0.99144, 0.99356, 0.99337, 0.99326, 0.99285, 0.99251, 0.99264, 0.99393, 0.99366, 0.9943, 0.9943, 0.99276, 0.99288, 0.99389, 0.99393, 0.99274, 0.99289, 0.99316, 0.9931, 0.99379, 0.99377, 0.99268, 0.99271, 0.99222, 0.99227, 0.99175, 0.9916, 0.91076, 0.91046, 0.98931, 0.98933, 0.99087, 0.99088, 0.98852, 0.98855, 0.98895, 0.98896, 0.98806, 0.98808, 0.9871, 0.98711, 0.98613, 0.98618, 0.98434, 0.98435, 0.983, 0.98307, 0.98185, 0.98187, 0.98131, 0.98131, 0.9783, 0.97835, 0.97619, 0.9762, 0.97264, 0.9727, 0.97088, 0.97098, 0.96568, 0.9658, 0.96045, 0.96055, 0.95322, 0.95335, 0.94579, 0.94594, 0.93297, 0.93311, 0.91699, 0.9172, 0.89174, 0.89202, 0.8541, 0.85446, 0.79823, 0.79902]
            elif base_model_type in ["ti2v_2_2"]:
                if inputs.get("image_start", None) is not None and inputs.get("video_source", None) is not None : # t2v
                    def_mag_ratios = [0.99505, 0.99389, 0.99441, 0.9957, 0.99558, 0.99551, 0.99499, 0.9945, 0.99534, 0.99548, 0.99468, 0.9946, 0.99463, 0.99458, 0.9946, 0.99453, 0.99408, 0.99404, 0.9945, 0.99441, 0.99409, 0.99398, 0.99403, 0.99397, 0.99382, 0.99377, 0.99349, 0.99343, 0.99377, 0.99378, 0.9933, 0.99328, 0.99303, 0.99301, 0.99217, 0.99216, 0.992, 0.99201, 0.99201, 0.99202, 0.99133, 0.99132, 0.99112, 0.9911, 0.99155, 0.99155, 0.98958, 0.98957, 0.98959, 0.98958, 0.98838, 0.98835, 0.98826, 0.98825, 0.9883, 0.98828, 0.98711, 0.98709, 0.98562, 0.98561, 0.98511, 0.9851, 0.98414, 0.98412, 0.98284, 0.98282, 0.98104, 0.98101, 0.97981, 0.97979, 0.97849, 0.97849, 0.97557, 0.97554, 0.97398, 0.97395, 0.97171, 0.97166, 0.96917, 0.96913, 0.96511, 0.96507, 0.96263, 0.96257, 0.95839, 0.95835, 0.95483, 0.95475, 0.94942, 0.94936, 0.9468, 0.94678, 0.94583, 0.94594, 0.94843, 0.94872, 0.96949, 0.97015]
                else: # i2v
                    def_mag_ratios = [0.99512, 0.99559, 0.99559, 0.99561, 0.99595, 0.99577, 0.99512, 0.99512, 0.99546, 0.99534, 0.99543, 0.99531, 0.99496, 0.99491, 0.99504, 0.99499, 0.99444, 0.99449, 0.99481, 0.99481, 0.99435, 0.99435, 0.9943, 0.99431, 0.99411, 0.99406, 0.99373, 0.99376, 0.99413, 0.99405, 0.99363, 0.99359, 0.99335, 0.99331, 0.99244, 0.99243, 0.99229, 0.99229, 0.99239, 0.99236, 0.99163, 0.9916, 0.99149, 0.99151, 0.99191, 0.99192, 0.9898, 0.98981, 0.9899, 0.98987, 0.98849, 0.98849, 0.98846, 0.98846, 0.98861, 0.98861, 0.9874, 0.98738, 0.98588, 0.98589, 0.98539, 0.98534, 0.98444, 0.98439, 0.9831, 0.98309, 0.98119, 0.98118, 0.98001, 0.98, 0.97862, 0.97859, 0.97555, 0.97558, 0.97392, 0.97388, 0.97152, 0.97145, 0.96871, 0.9687, 0.96435, 0.96434, 0.96129, 0.96127, 0.95639, 0.95638, 0.95176, 0.95175, 0.94446, 0.94452, 0.93972, 0.93974, 0.93575, 0.9359, 0.93537, 0.93552, 0.96655, 0.96616]
            elif test_class_1_3B(base_model_type): #text 1.3B
                def_mag_ratios = [1.0124, 1.02213, 1.00166, 1.0041, 0.99791, 1.00061, 0.99682, 0.99762, 0.99634, 0.99685, 0.99567, 0.99586, 0.99416, 0.99422, 0.99578, 0.99575, 0.9957, 0.99563, 0.99511, 0.99506, 0.99535, 0.99531, 0.99552, 0.99549, 0.99541, 0.99539, 0.9954, 0.99536, 0.99489, 0.99485, 0.99518, 0.99514, 0.99484, 0.99478, 0.99481, 0.99479, 0.99415, 0.99413, 0.99419, 0.99416, 0.99396, 0.99393, 0.99388, 0.99386, 0.99349, 0.99349, 0.99309, 0.99304, 0.9927, 0.9927, 0.99228, 0.99226, 0.99171, 0.9917, 0.99137, 0.99135, 0.99068, 0.99063, 0.99005, 0.99003, 0.98944, 0.98942, 0.98849, 0.98849, 0.98758, 0.98757, 0.98644, 0.98643, 0.98504, 0.98503, 0.9836, 0.98359, 0.98202, 0.98201, 0.97977, 0.97978, 0.97717, 0.97718, 0.9741, 0.97411, 0.97003, 0.97002, 0.96538, 0.96541, 0.9593, 0.95933, 0.95086, 0.95089, 0.94013, 0.94019, 0.92402, 0.92414, 0.90241, 0.9026, 0.86821, 0.86868, 0.81838, 0.81939]#**(0.5)# In our papaer, we utilize the sqrt to smooth the ratio, which has little impact on the performance and can be deleted.
            elif i2v:
                if pixels >= 1280*720:
                    def_mag_ratios = [0.99428, 0.99498, 0.98588, 0.98621, 0.98273, 0.98281, 0.99018, 0.99023, 0.98911, 0.98917, 0.98646, 0.98652, 0.99454, 0.99456, 0.9891, 0.98909, 0.99124, 0.99127, 0.99102, 0.99103, 0.99215, 0.99212, 0.99515, 0.99515, 0.99576, 0.99572, 0.99068, 0.99072, 0.99097, 0.99097, 0.99166, 0.99169, 0.99041, 0.99042, 0.99201, 0.99198, 0.99101, 0.99101, 0.98599, 0.98603, 0.98845, 0.98844, 0.98848, 0.98851, 0.98862, 0.98857, 0.98718, 0.98719, 0.98497, 0.98497, 0.98264, 0.98263, 0.98389, 0.98393, 0.97938, 0.9794, 0.97535, 0.97536, 0.97498, 0.97499, 0.973, 0.97301, 0.96827, 0.96828, 0.96261, 0.96263, 0.95335, 0.9534, 0.94649, 0.94655, 0.93397, 0.93414, 0.91636, 0.9165, 0.89088, 0.89109, 0.8679, 0.86768]
                else:
                    def_mag_ratios =  [0.98783, 0.98993, 0.97559, 0.97593, 0.98311, 0.98319, 0.98202, 0.98225, 0.9888, 0.98878, 0.98762, 0.98759, 0.98957, 0.98971, 0.99052, 0.99043, 0.99383, 0.99384, 0.98857, 0.9886, 0.99065, 0.99068, 0.98845, 0.98847, 0.99057, 0.99057, 0.98957, 0.98961, 0.98601, 0.9861, 0.98823, 0.98823, 0.98756, 0.98759, 0.98808, 0.98814, 0.98721, 0.98724, 0.98571, 0.98572, 0.98543, 0.98544, 0.98157, 0.98165, 0.98411, 0.98413, 0.97952, 0.97953, 0.98149, 0.9815, 0.9774, 0.97742, 0.97825, 0.97826, 0.97355, 0.97361, 0.97085, 0.97087, 0.97056, 0.97055, 0.96588, 0.96587, 0.96113, 0.96124, 0.9567, 0.95681, 0.94961, 0.94969, 0.93973, 0.93988, 0.93217, 0.93224, 0.91878, 0.91896, 0.90955, 0.90954, 0.92617, 0.92616]
            else: # text 14B
                def_mag_ratios = [1.02504, 1.03017, 1.00025, 1.00251, 0.9985, 0.99962, 0.99779, 0.99771, 0.9966, 0.99658, 0.99482, 0.99476, 0.99467, 0.99451, 0.99664, 0.99656, 0.99434, 0.99431, 0.99533, 0.99545, 0.99468, 0.99465, 0.99438, 0.99434, 0.99516, 0.99517, 0.99384, 0.9938, 0.99404, 0.99401, 0.99517, 0.99516, 0.99409, 0.99408, 0.99428, 0.99426, 0.99347, 0.99343, 0.99418, 0.99416, 0.99271, 0.99269, 0.99313, 0.99311, 0.99215, 0.99215, 0.99218, 0.99215, 0.99216, 0.99217, 0.99163, 0.99161, 0.99138, 0.99135, 0.98982, 0.9898, 0.98996, 0.98995, 0.9887, 0.98866, 0.98772, 0.9877, 0.98767, 0.98765, 0.98573, 0.9857, 0.98501, 0.98498, 0.9838, 0.98376, 0.98177, 0.98173, 0.98037, 0.98035, 0.97678, 0.97677, 0.97546, 0.97543, 0.97184, 0.97183, 0.96711, 0.96708, 0.96349, 0.96345, 0.95629, 0.95625, 0.94926, 0.94929, 0.93964, 0.93961, 0.92511, 0.92504, 0.90693, 0.90678, 0.8796, 0.87945, 0.86111, 0.86189]
            skip_steps_cache.def_mag_ratios = def_mag_ratios
        else:
            if i2v:
                if pixels >= 1280*720:
                    coefficients= [-114.36346466,   65.26524496,  -18.82220707,    4.91518089,   -0.23412683]
                else:
                    coefficients= [-3.02331670e+02,  2.23948934e+02, -5.25463970e+01,  5.87348440e+00, -2.01973289e-01]
            else:
                if test_class_1_3B(base_model_type):
                    coefficients= [2.39676752e+03, -1.31110545e+03,  2.01331979e+02, -8.29855975e+00, 1.37887774e-01]
                else: 
                    coefficients= [-5784.54975374,  5449.50911966, -1811.16591783,   256.27178429, -13.02252404]
            skip_steps_cache.coefficients = coefficients

    @staticmethod
    def get_wan_text_encoder_filename(text_encoder_quantization):
        text_encoder_filename = "ckpts/umt5-xxl/models_t5_umt5-xxl-enc-bf16.safetensors"
        if text_encoder_quantization =="int8":
            text_encoder_filename = text_encoder_filename.replace("bf16", "quanto_int8") 
        return text_encoder_filename



    @staticmethod
    def query_modules_files():
        return {
            "vace_14B" : ["ckpts/wan2.1_Vace_14B_module_mbf16.safetensors", "ckpts/wan2.1_Vace_14B_module_quanto_mbf16_int8.safetensors", "ckpts/wan2.1_Vace_14B_module_quanto_mfp16_int8.safetensors"],
            "vace_1.3B" : ["ckpts/wan2.1_Vace_1_3B_module.safetensors"],
            "fantasy": ["ckpts/wan2.1_fantasy_speaking_14B_bf16.safetensors"],
            "multitalk": ["ckpts/wan2.1_multitalk_14B_mbf16.safetensors", "ckpts/wan2.1_multitalk_14B_quanto_mbf16_int8.safetensors", "ckpts/wan2.1_multitalk_14B_quanto_mfp16_int8.safetensors"]
}

    @staticmethod
    def query_model_def(base_model_type, model_def):
        extra_model_def = {}
        if "URLs2" in model_def:
            extra_model_def["no_steps_skipping"] = True
        i2v =  test_class_i2v(base_model_type)
        extra_model_def["i2v_class"] = i2v
        extra_model_def["multitalk_class"] = base_model_type in ["multitalk", "vace_multitalk_14B", "i2v_2_2_multitalk"]
        vace_class = base_model_type in ["vace_14B", "vace_1.3B", "vace_multitalk_14B"] 
        extra_model_def["vace_class"] = vace_class

        if base_model_type in ["multitalk", "vace_multitalk_14B", "i2v_2_2_multitalk"]:
            fps = 25
        elif base_model_type in ["fantasy"]:
            fps = 23
        elif base_model_type in ["ti2v_2_2"]:
            fps = 24
        else:
            fps = 16
        extra_model_def["fps"] =fps

        if vace_class: 
            frames_minimum, frames_steps =  17, 4
        else:
            frames_minimum, frames_steps = 5, 4
        extra_model_def.update({
        "frames_minimum" : frames_minimum,
        "frames_steps" : frames_steps, 
        "sliding_window" : base_model_type in ["multitalk", "t2v", "fantasy"] or test_class_i2v(base_model_type) or vace_class,  #"ti2v_2_2",
        "guidance_max_phases" : 2,
        "skip_layer_guidance" : True,        
        "cfg_zero" : True,
        "cfg_star" : True,
        "adaptive_projected_guidance" : True,  
        "tea_cache" : not (base_model_type in ["i2v_2_2", "ti2v_2_2" ] or "URLs2" in model_def),
        "mag_cache" : True,
        "sample_solvers":[
                            ("unipc", "unipc"),
                            ("euler", "euler"),
                            ("dpm++", "dpm++"),
                            ("flowmatch causvid", "causvid"), ]
        })

        return extra_model_def
        
    @staticmethod
    def query_supported_types():
        return ["multitalk", "fantasy", "vace_14B", "vace_multitalk_14B",
                    "t2v_1.3B", "t2v", "vace_1.3B", "phantom_1.3B", "phantom_14B", 
                    "recam_1.3B", 
                    "i2v", "i2v_2_2", "i2v_2_2_multitalk", "ti2v_2_2", "flf2v_720p", "fun_inp_1.3B", "fun_inp"]


    @staticmethod
    def query_family_maps():

        models_eqv_map = {
            "flf2v_720p" : "i2v",
            "t2v_1.3B" : "t2v",
        }

        models_comp_map = { 
                    "vace_14B" : [ "vace_multitalk_14B"],
                    "t2v" : [ "vace_14B", "vace_1.3B" "vace_multitalk_14B", "t2v_1.3B", "phantom_1.3B","phantom_14B"],
                    "i2v" : [ "fantasy", "multitalk", "flf2v_720p" ],
                    "i2v_2_2" : ["i2v_2_2_multitalk"],
                    "fantasy": ["multitalk"],
                    }
        return models_eqv_map, models_comp_map

    @staticmethod
    def query_model_family():
        return "wan"
    
    @staticmethod
    def query_family_infos():
        return {"wan":(0, "Wan2.1"), "wan2_2":(1, "Wan2.2") }

    @staticmethod
    def get_vae_block_size(base_model_type):
        return 32 if base_model_type == "ti2v_2_2" else 16

    @staticmethod
    def get_rgb_factors(base_model_type ):
        from shared.RGB_factors import get_rgb_factors
        if base_model_type == "ti2v_2_2": return None, None
        latent_rgb_factors, latent_rgb_factors_bias = get_rgb_factors("wan")
        return latent_rgb_factors, latent_rgb_factors_bias
    
    @staticmethod
    def query_model_files(computeList, base_model_type, model_filename, text_encoder_quantization):
        text_encoder_filename = family_handler.get_wan_text_encoder_filename(text_encoder_quantization)

        download_def  = [{
            "repoId" : "DeepBeepMeep/Wan2.1", 
            "sourceFolderList" :  ["xlm-roberta-large", "umt5-xxl", ""  ],
            "fileList" : [ [ "models_clip_open-clip-xlm-roberta-large-vit-huge-14-bf16.safetensors", "sentencepiece.bpe.model", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"], ["special_tokens_map.json", "spiece.model", "tokenizer.json", "tokenizer_config.json"] + computeList(text_encoder_filename) , ["Wan2.1_VAE.safetensors",  "fantasy_proj_model.safetensors" ] +  computeList(model_filename)  ]   
        }]

        if base_model_type == "ti2v_2_2":
            download_def += [    {
                "repoId" : "DeepBeepMeep/Wan2.2", 
                "sourceFolderList" :  [""],
                "fileList" : [ [ "Wan2.2_VAE.safetensors" ]  ]
            }]

        return download_def


    @staticmethod
    def load_model(model_filename, model_type, base_model_type, model_def, quantizeTransformer = False, text_encoder_quantization = None, dtype = torch.bfloat16, VAE_dtype = torch.float32, mixed_precision_transformer = False, save_quantized= False):
        from .configs import WAN_CONFIGS

        if test_class_i2v(base_model_type):
            cfg = WAN_CONFIGS['i2v-14B']
        else:
            cfg = WAN_CONFIGS['t2v-14B']
            # cfg = WAN_CONFIGS['t2v-1.3B']    
        from . import WanAny2V
        wan_model = WanAny2V(
            config=cfg,
            checkpoint_dir="ckpts",
            model_filename=model_filename,
            model_type = model_type,        
            model_def = model_def,
            base_model_type=base_model_type,
            text_encoder_filename= family_handler.get_wan_text_encoder_filename(text_encoder_quantization),
            quantizeTransformer = quantizeTransformer,
            dtype = dtype,
            VAE_dtype = VAE_dtype, 
            mixed_precision_transformer = mixed_precision_transformer,
            save_quantized = save_quantized
        )

        pipe = {"transformer": wan_model.model, "text_encoder" : wan_model.text_encoder.model, "vae": wan_model.vae.model }
        if hasattr(wan_model,"model2") and wan_model.model2 is not None:
            pipe["transformer2"] = wan_model.model2
        if hasattr(wan_model, "clip"):
            pipe["text_encoder_2"] = wan_model.clip.model
        return wan_model, pipe

    @staticmethod
    def fix_settings(base_model_type, settings_version, model_def, ui_defaults):
        if ui_defaults.get("sample_solver", "") == "": 
            ui_defaults["sample_solver"] = "unipc"

    @staticmethod
    def update_default_settings(base_model_type, model_def, ui_defaults):
        ui_defaults.update({
            "sample_solver": "unipc",
        })
        if base_model_type in ["fantasy"]:
            ui_defaults.update({
                "audio_guidance_scale": 5.0,
                "sliding_window_size": 1, 
            })

        elif base_model_type in ["multitalk"]:
            ui_defaults.update({
                "guidance_scale": 5.0,
                "flow_shift": 7, # 11 for 720p
                "audio_guidance_scale": 4,
                "sliding_window_discard_last_frames" : 4,
                "sample_solver" : "euler",
                "adaptive_switch" : 1,
            })

        elif base_model_type in ["phantom_1.3B", "phantom_14B"]:
            ui_defaults.update({
                "guidance_scale": 7.5,
                "flow_shift": 5,
                "remove_background_images_ref": 1,
                "video_prompt_type": "I",
                # "resolution": "1280x720" 
            })

        elif base_model_type in ["vace_14B", "vace_multitalk_14B"]:
            ui_defaults.update({
                "sliding_window_discard_last_frames": 0,
            })

        elif base_model_type in ["ti2v_2_2"]:
            ui_defaults.update({
                "image_prompt_type": "T", 
            })

            