# Copyright 2024-2025 The Alibaba Wan Team Authors. All rights reserved.
##### Enjoy this spagheti VRAM optimizations done by DeepBeepMeep !
# I am sure you are a nice person and as you copy this code, you will give me officially proper credits:
# Please link to https://github.com/deepbeepmeep/Wan2GP and @deepbeepmeep on twitter  
import math
from einops import rearrange
import torch
import torch.cuda.amp as amp
import torch.nn as nn
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
import numpy as np
from typing import Union,Optional
from mmgp import offload
from shared.attention import pay_attention
from torch.backends.cuda import sdp_kernel
from ..multitalk.multitalk_utils import get_attn_map_with_target

__all__ = ['WanModel']


def sinusoidal_embedding_1d(dim, position):
    # preprocess
    assert dim % 2 == 0
    half = dim // 2
    position = position.type(torch.float32)

    # calculation
    sinusoid = torch.outer(
        position, torch.pow(10000, -torch.arange(half).to(position).div(half)))
    x = torch.cat([torch.cos(sinusoid), torch.sin(sinusoid)], dim=1)
    return x


def reshape_latent(latent, latent_frames):
    return latent.reshape(latent.shape[0], latent_frames, -1, latent.shape[-1] )

def restore_latent_shape(latent):
    return latent.reshape(latent.shape[0], -1, latent.shape[-1] )


def identify_k( b: float, d: int, N: int):
    """
    This function identifies the index of the intrinsic frequency component in a RoPE-based pre-trained diffusion transformer.

    Args:
        b (`float`): The base frequency for RoPE.
        d (`int`): Dimension of the frequency tensor
        N (`int`): the first observed repetition frame in latent space
    Returns:
        k (`int`): the index of intrinsic frequency component
        N_k (`int`): the period of intrinsic frequency component in latent space
    Example:
        In HunyuanVideo, b=256 and d=16, the repetition occurs approximately 8s (N=48 in latent space).
        k, N_k = identify_k(b=256, d=16, N=48)
        In this case, the intrinsic frequency index k is 4, and the period N_k is 50.
    """

    # Compute the period of each frequency in RoPE according to Eq.(4)
    periods = []
    for j in range(1, d // 2 + 1):
        theta_j = 1.0 / (b ** (2 * (j - 1) / d))
        N_j = round(2 * torch.pi / theta_j)
        periods.append(N_j)

    # Identify the intrinsic frequency whose period is closed to N（see Eq.(7)）
    diffs = [abs(N_j - N) for N_j in periods]
    k = diffs.index(min(diffs)) + 1
    N_k = periods[k-1]
    return k, N_k

def rope_params_riflex(max_seq_len, dim, theta=10000, L_test=30, k=6):
    assert dim % 2 == 0
    exponents = torch.arange(0, dim, 2, dtype=torch.float64).div(dim)
    inv_theta_pow = 1.0 / torch.pow(theta, exponents)
    
    inv_theta_pow[k-1] = 0.9 * 2 * torch.pi / L_test
        
    freqs = torch.outer(torch.arange(max_seq_len), inv_theta_pow)
    if True:
        freqs_cos = freqs.cos().repeat_interleave(2, dim=1).float()  # [S, D]
        freqs_sin = freqs.sin().repeat_interleave(2, dim=1).float()  # [S, D]
        return (freqs_cos, freqs_sin)
    else:
        freqs = torch.polar(torch.ones_like(freqs), freqs)  # complex64     # [S, D/2]
    return freqs



def relative_l1_distance(last_tensor, current_tensor):
    l1_distance = torch.abs(last_tensor - current_tensor).mean()
    norm = torch.abs(last_tensor).mean()
    relative_l1_distance = l1_distance / norm
    return relative_l1_distance.to(torch.float32)

class WanRMSNorm(nn.Module):

    def __init__(self, dim, eps=1e-5):
        super().__init__()
        self.dim = dim
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
        """
        y = x.float()
        y.pow_(2)
        y = y.mean(dim=-1, keepdim=True)
        y += self.eps
        y.rsqrt_()
        x *=  y
        x *= self.weight
        return x
        # return self._norm(x).type_as(x) * self.weight

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(dim=-1, keepdim=True) + self.eps)

def my_LayerNorm(norm, x):
        y = x.float()
        y_m = y.mean(dim=-1, keepdim=True)
        y -= y_m 
        del y_m
        y.pow_(2)
        y = y.mean(dim=-1, keepdim=True)
        y += norm.eps
        y.rsqrt_()
        x = x *  y
        return x


class WanLayerNorm(nn.LayerNorm):

    def __init__(self, dim, eps=1e-6, elementwise_affine=False):
        super().__init__(dim, elementwise_affine=elementwise_affine, eps=eps)

    def forward(self, x):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
        """
        # return F.layer_norm(
        #     input, self.normalized_shape, self.weight, self.bias, self.eps
        # )
        y = super().forward(x)
        x = y.type_as(x)
        return x
        # return super().forward(x).type_as(x)

from .posemb_layers import apply_rotary_emb

class WanSelfAttention(nn.Module):

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 eps=1e-6,
                 block_no=0):
        assert dim % num_heads == 0
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.eps = eps
        self.block_no = block_no

        # layers
        self.q = nn.Linear(dim, dim)
        self.k = nn.Linear(dim, dim)
        self.v = nn.Linear(dim, dim)
        self.o = nn.Linear(dim, dim)
        self.norm_q = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()
        self.norm_k = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()


    def text_cross_attention(self, xlist, context, return_q = False):
        x = xlist[0]
        xlist.clear()
        b, n, d = x.size(0), self.num_heads, self.head_dim
        nag_scale = offload.shared_state.get("_nag_scale",0)
        # compute query, key, value
        q = self.q(x)
        del x
        self.norm_q(q)
        q= q.view(b, -1, n, d)
        k = self.k(context)
        self.norm_k(k)
        k = k.view(context.shape[0], -1, n, d)
        v = self.v(context).view(context.shape[0], -1, n, d)

        if nag_scale <= 1 or len(k)==1:
            qvl_list=[q, k, v]
            if not return_q: del q
            del k, v
            x = pay_attention(qvl_list,  cross_attn= True)
            x = x.flatten(2, 3)
        else:
            nag_tau = offload.shared_state["_nag_tau"]
            nag_alpha = offload.shared_state["_nag_alpha"]
            qvl_list=[q, k[:1], v[:1]]
            x_pos = pay_attention(qvl_list,  cross_attn= True)
            qvl_list=[q, k[1:], v[1:]]
            if not return_q: del q
            del k, v
            x_neg = pay_attention(qvl_list,  cross_attn= True)

            x_pos = x_pos.flatten(2, 3)
            x_neg = x_neg.flatten(2, 3)
            # Behold DeepBeepMeep as the NAG Butcher !: reduce highly VRAM consumption while at the same time turn the source in gibberish
            x_neg.mul_(1-nag_scale)
            x_neg.add_(x_pos, alpha= nag_scale)
            x_guidance = x_neg
            del x_neg
            norm_positive = torch.norm(x_pos, p=1, dim=-1, keepdim=True)
            norm_guidance = torch.norm(x_guidance, p=1, dim=-1, keepdim=True)
            scale = norm_guidance / norm_positive
            scale = torch.nan_to_num(scale, 10)
            factor = 1 / (norm_guidance + 1e-7) * norm_positive * nag_tau
            x_guidance = torch.where(scale > nag_tau, x_guidance * factor, x_guidance )
            del norm_positive, norm_guidance 
            x_pos.mul_(1 - nag_alpha)
            x_guidance.mul_(nag_alpha)
            x_guidance.add_(x_pos)
            x = x_guidance

            # x_guidance = x_pos * nag_scale - x_neg * (nag_scale - 1)
            # norm_positive = torch.norm(x_pos, p=1, dim=-1, keepdim=True).expand(*x_pos.shape)
            # norm_guidance = torch.norm(x_guidance, p=1, dim=-1, keepdim=True).expand(*x_guidance.shape)

            # scale = norm_guidance / norm_positive
            # scale = torch.nan_to_num(scale, 10)
            # x_guidance[scale > nag_tau] =  x_guidance[scale > nag_tau] / (norm_guidance[scale > nag_tau] + 1e-7) * norm_positive[scale > nag_tau] * nag_tau

            # x = x_guidance * nag_alpha + x_pos * (1 - nag_alpha)
        if return_q:
            return x, q
        else:
            return x, None
    
    def forward(self, xlist, grid_sizes, freqs, block_mask = None, ref_target_masks = None, ref_images_count = 0):
        r"""
        Args:
            x(Tensor): Shape [B, L, num_heads, C / num_heads]
            grid_sizes(Tensor): Shape [B, 3], the second dimension contains (F, H, W)
            freqs(Tensor): Rope freqs, shape [1024, C / num_heads / 2]
        """
        x = xlist[0]
        xlist.clear()

        b, s, n, d = *x.shape[:2], self.num_heads, self.head_dim

        # query, key, value function
        q = self.q(x)
        self.norm_q(q)
        q = q.view(b, s, n, d) 
        k = self.k(x)
        self.norm_k(k)
        k = k.view(b, s, n, d) 
        v = self.v(x).view(b, s, n, d)
        del x
        qklist = [q,k]
        del q,k

        q,k = apply_rotary_emb(qklist, freqs, head_first=False)

        if ref_target_masks != None:
            x_ref_attn_map = get_attn_map_with_target(q, k , grid_sizes, ref_target_masks=ref_target_masks, ref_images_count = ref_images_count)
        else:
            x_ref_attn_map = None

        chipmunk = offload.shared_state.get("_chipmunk", False) 
        if chipmunk and self.__class__ == WanSelfAttention:
            q = q.transpose(1,2)
            k = k.transpose(1,2)
            v = v.transpose(1,2)
            attn_layers = offload.shared_state["_chipmunk_layers"]
            x = attn_layers[self.block_no](q, k, v)
            x = x.transpose(1,2)
        elif block_mask == None:
            qkv_list = [q,k,v]
            del q,k,v
            x = pay_attention(
                qkv_list,
                window_size=self.window_size)
        else:
            with sdp_kernel(enable_flash=True, enable_math=False, enable_mem_efficient=False):
                x = (
                    torch.nn.functional.scaled_dot_product_attention(
                        q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), attn_mask=block_mask
                    )
                    .transpose(1, 2)
                    .contiguous()
                )
                del q,k,v


        x = x.flatten(2)
        x = self.o(x)
        return x, x_ref_attn_map


class WanT2VCrossAttention(WanSelfAttention):

    def forward(self, xlist, context, grid_sizes, *args, **kwargs):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            context(Tensor): Shape [B, L2, C]
        """
        x, _ = self.text_cross_attention( xlist, context)
        x = self.o(x)
        return x


class WanI2VCrossAttention(WanSelfAttention):

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 eps=1e-6,
                 block_no=0):
        super().__init__(dim, num_heads, window_size, qk_norm, eps, block_no)

        self.k_img = nn.Linear(dim, dim)
        self.v_img = nn.Linear(dim, dim)
        # self.alpha = nn.Parameter(torch.zeros((1, )))
        self.norm_k_img = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()

    def forward(self, xlist, context, grid_sizes, audio_proj, audio_scale, audio_context_lens ):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            context(Tensor): Shape [B, L2, C]
        """


        context_img = context[:, :257]
        context = context[:, 257:]
        
        x, q = self.text_cross_attention( xlist, context, return_q = True)
        if len(q) != len(context_img):
            context_img = context_img[:len(q)]
        b, s, n, d = *x.shape[:2], self.num_heads, self.head_dim

        if audio_scale != None:
            audio_x = self.processor(q, audio_proj, grid_sizes[0], audio_context_lens)
        k_img = self.k_img(context_img)
        self.norm_k_img(k_img)
        k_img = k_img.view(b, -1, n, d)
        v_img = self.v_img(context_img).view(b, -1, n, d)
        qkv_list = [q, k_img, v_img]
        del q, k_img, v_img
        img_x = pay_attention(qkv_list)
        img_x = img_x.flatten(2)

        # output
        x += img_x
        del img_x
        if audio_scale != None:
            x.add_(audio_x, alpha= audio_scale)
        x = self.o(x)
        return x



WAN_CROSSATTENTION_CLASSES = {
    't2v_cross_attn': WanT2VCrossAttention,
    'i2v_cross_attn': WanI2VCrossAttention,
}


class WanAttentionBlock(nn.Module):

    def __init__(self,
                 cross_attn_type,
                 dim,
                 ffn_dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 cross_attn_norm=False,
                 eps=1e-6,
                 block_id=None,
                 block_no = 0,                 
                 output_dim=0,
                 norm_input_visual=True,
                 class_range=24,
                 class_interval=4,
                 ):
        super().__init__()
        self.dim = dim
        self.ffn_dim = ffn_dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.cross_attn_norm = cross_attn_norm
        self.eps = eps
        self.block_no = block_no

        # layers
        self.norm1 = WanLayerNorm(dim, eps)
        self.self_attn = WanSelfAttention(dim, num_heads, window_size, qk_norm,
                                          eps, block_no= block_no)
        self.norm3 = WanLayerNorm(
            dim, eps,
            elementwise_affine=True) if cross_attn_norm else nn.Identity()
        self.cross_attn = WAN_CROSSATTENTION_CLASSES[cross_attn_type](dim,
                                                                      num_heads,
                                                                      (-1, -1),
                                                                      qk_norm,
                                                                      eps, 
                                                                      block_no)
        self.norm2 = WanLayerNorm(dim, eps)
        self.ffn = nn.Sequential(
            nn.Linear(dim, ffn_dim), nn.GELU(approximate='tanh'),
            nn.Linear(ffn_dim, dim))

        # modulation
        self.modulation = nn.Parameter(torch.randn(1, 6, dim) / dim**0.5)
        self.block_id = block_id

        if output_dim > 0:
            from ..multitalk.attention import SingleStreamMutiAttention 
            # init audio module
            self.audio_cross_attn = SingleStreamMutiAttention(
                    dim=dim,
                    encoder_hidden_states_dim=output_dim,
                    num_heads=num_heads,
                    qk_norm=False,
                    qkv_bias=True,
                    eps=eps,
                    norm_layer=WanRMSNorm,
                    class_range=class_range,
                    class_interval=class_interval
                )
            self.norm_x = WanLayerNorm(dim, eps, elementwise_affine=True)  if norm_input_visual else nn.Identity()
    
    def forward(
        self,
        x,
        e,
        grid_sizes,
        freqs,
        context,
        hints= None, 
        context_scale=[1.0],
        cam_emb= None,
        block_mask = None,
        audio_proj= None,
        audio_context_lens= None,
        audio_scale=None,
        multitalk_audio=None,
        multitalk_masks=None,
        ref_images_count=0,
    ):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
            e(Tensor): Shape [B, 6, C]
            grid_sizes(Tensor): Shape [B, 3], the second dimension contains (F, H, W)
            freqs(Tensor): Rope freqs, shape [1024, C / num_heads / 2]
        """
        hints_processed = None
        attention_dtype =  self.self_attn.q.weight.dtype 
        dtype = x.dtype

        if self.block_id is not None and hints is not None:
            kwargs = { 
                "grid_sizes" : grid_sizes,
                "freqs" :freqs, 
                "context" : context,
                "e" : e,
            }
            hints_processed= []
            for scale, hint in zip(context_scale, hints):
                if scale == 0:
                    hints_processed.append(None)
                else:
                    hints_processed.append(self.vace(hint, x, **kwargs) if self.block_id == 0 else self.vace(hint, None, **kwargs))
                     
        latent_frames = e.shape[0]
        e = (self.modulation + e).chunk(6, dim=1)
        # self-attention
        x_mod = self.norm1(x)
        x_mod = reshape_latent(x_mod , latent_frames)
        x_mod *= 1 + e[1]
        x_mod += e[0]
        x_mod = restore_latent_shape(x_mod)
        if cam_emb != None:
            cam_emb = self.cam_encoder(cam_emb)
            cam_emb = cam_emb.repeat(1, 2, 1)
            cam_emb = cam_emb.unsqueeze(2).unsqueeze(3).repeat(1, 1, grid_sizes[1], grid_sizes[2], 1)
            cam_emb = rearrange(cam_emb, 'b f h w d -> b (f h w) d')
            x_mod += cam_emb

        xlist = [x_mod.to(attention_dtype)]
        del x_mod
        y, x_ref_attn_map = self.self_attn( xlist, grid_sizes, freqs, block_mask = block_mask, ref_target_masks = multitalk_masks, ref_images_count = ref_images_count)
        y = y.to(dtype)

        if cam_emb != None: y = self.projector(y)

        x, y = reshape_latent(x , latent_frames), reshape_latent(y , latent_frames)
        x.addcmul_(y, e[2])
        x, y = restore_latent_shape(x), restore_latent_shape(y)
        del y
        y = self.norm3(x)
        y = y.to(attention_dtype)
        ylist= [y]
        del y
        x += self.cross_attn(ylist, context, grid_sizes, audio_proj, audio_scale, audio_context_lens).to(dtype)

        if multitalk_audio != None:
            # cross attn of multitalk audio
            y = self.norm_x(x)
            y = y.to(attention_dtype)
            if ref_images_count == 0:
                x += self.audio_cross_attn(y, encoder_hidden_states=multitalk_audio, shape=grid_sizes, x_ref_attn_map=x_ref_attn_map)
            else:
                y_shape = y.shape
                y = y.reshape(y_shape[0], grid_sizes[0], -1)
                y = y[:, ref_images_count:]
                y = y.reshape(y_shape[0], -1, y_shape[-1])
                grid_sizes_alt = [grid_sizes[0]-ref_images_count, *grid_sizes[1:]]
                y = self.audio_cross_attn(y, encoder_hidden_states=multitalk_audio, shape=grid_sizes_alt, x_ref_attn_map=x_ref_attn_map)
                y = y.reshape(y_shape[0], grid_sizes[0]-ref_images_count, -1)
                x = x.reshape(y_shape[0], grid_sizes[0], -1)
                x[:, ref_images_count:] += y
                x = x.reshape(y_shape[0], -1, y_shape[-1])
            del y

        y = self.norm2(x)

        y = reshape_latent(y , latent_frames)
        y *= 1 + e[4]
        y += e[3]
        y = restore_latent_shape(y)
        y = y.to(attention_dtype)

        ffn = self.ffn[0]
        gelu = self.ffn[1]
        ffn2= self.ffn[2]

        y_shape = y.shape
        y = y.view(-1, y_shape[-1])
        chunk_size = int(y.shape[0]/2.7)
        chunks =torch.split(y, chunk_size)
        for y_chunk  in chunks:
            mlp_chunk = ffn(y_chunk)
            mlp_chunk = gelu(mlp_chunk)
            y_chunk[...] = ffn2(mlp_chunk)
            del mlp_chunk 
        y = y.view(y_shape)
        y = y.to(dtype)
        x, y = reshape_latent(x , latent_frames), reshape_latent(y , latent_frames)
        x.addcmul_(y, e[5])
        x, y = restore_latent_shape(x), restore_latent_shape(y)

        if hints_processed is not None:
            for hint, scale in zip(hints_processed, context_scale):
                if scale != 0:
                    if scale == 1:
                        x.add_(hint)
                    else:
                        x.add_(hint, alpha= scale)
        return x 

class AudioProjModel(ModelMixin, ConfigMixin):
    def __init__(
        self,
        seq_len=5,
        seq_len_vf=12,
        blocks=12,  
        channels=768, 
        intermediate_dim=512,
        output_dim=768,
        context_tokens=32,
        norm_output_audio=False,
    ):
        super().__init__()

        self.seq_len = seq_len
        self.blocks = blocks
        self.channels = channels
        self.input_dim = seq_len * blocks * channels  
        self.input_dim_vf = seq_len_vf * blocks * channels
        self.intermediate_dim = intermediate_dim
        self.context_tokens = context_tokens
        self.output_dim = output_dim

        # define multiple linear layers
        self.proj1 = nn.Linear(self.input_dim, intermediate_dim)
        self.proj1_vf = nn.Linear(self.input_dim_vf, intermediate_dim)
        self.proj2 = nn.Linear(intermediate_dim, intermediate_dim)
        self.proj3 = nn.Linear(intermediate_dim, context_tokens * output_dim)
        self.norm = nn.LayerNorm(output_dim) if norm_output_audio else nn.Identity()

    def forward(self, audio_embeds, audio_embeds_vf):
        video_length = audio_embeds.shape[1] + audio_embeds_vf.shape[1]
        B, _, _, S, C = audio_embeds.shape

        # process audio of first frame
        audio_embeds = rearrange(audio_embeds, "bz f w b c -> (bz f) w b c")
        batch_size, window_size, blocks, channels = audio_embeds.shape
        audio_embeds = audio_embeds.view(batch_size, window_size * blocks * channels)

        # process audio of latter frame
        audio_embeds_vf = rearrange(audio_embeds_vf, "bz f w b c -> (bz f) w b c")
        batch_size_vf, window_size_vf, blocks_vf, channels_vf = audio_embeds_vf.shape
        audio_embeds_vf = audio_embeds_vf.view(batch_size_vf, window_size_vf * blocks_vf * channels_vf)

        # first projection
        audio_embeds = torch.relu(self.proj1(audio_embeds)) 
        audio_embeds_vf = torch.relu(self.proj1_vf(audio_embeds_vf)) 
        audio_embeds = rearrange(audio_embeds, "(bz f) c -> bz f c", bz=B)
        audio_embeds_vf = rearrange(audio_embeds_vf, "(bz f) c -> bz f c", bz=B)
        audio_embeds_c = torch.concat([audio_embeds, audio_embeds_vf], dim=1) 
        audio_embeds_vf = audio_embeds = None
        batch_size_c, N_t, C_a = audio_embeds_c.shape
        audio_embeds_c = audio_embeds_c.view(batch_size_c*N_t, C_a)

        # second projection
        audio_embeds_c = torch.relu(self.proj2(audio_embeds_c))

        context_tokens = self.proj3(audio_embeds_c).reshape(batch_size_c*N_t, self.context_tokens, self.output_dim)
        audio_embeds_c = None
        # normalization and reshape
        context_tokens = self.norm(context_tokens)
        context_tokens = rearrange(context_tokens, "(bz f) m c -> bz f m c", f=video_length)

        return context_tokens



class VaceWanAttentionBlock(WanAttentionBlock):
    def __init__(
            self,
            cross_attn_type,
            dim,
            ffn_dim,
            num_heads,
            window_size=(-1, -1),
            qk_norm=True,
            cross_attn_norm=False,
            eps=1e-6,
            block_id=0
    ):
        super().__init__(cross_attn_type, dim, ffn_dim, num_heads, window_size, qk_norm, cross_attn_norm, eps)
        self.block_id = block_id
        if block_id == 0:
            self.before_proj = nn.Linear(self.dim, self.dim)
            nn.init.zeros_(self.before_proj.weight)
            nn.init.zeros_(self.before_proj.bias)
        self.after_proj = nn.Linear(self.dim, self.dim)
        nn.init.zeros_(self.after_proj.weight)
        nn.init.zeros_(self.after_proj.bias)

    def forward(self, hints, x, **kwargs):
        # behold dbm magic !
        c = hints[0]
        hints[0] = None
        if self.block_id == 0:
            c = self.before_proj(c)
            bz = x.shape[0]
            if bz > c.shape[0]: c = c.repeat(bz, 1, 1 )
            c += x
        c = super().forward(c, **kwargs)
        c_skip = self.after_proj(c)
        hints[0] = c
        return c_skip

    
class Head(nn.Module):

    def __init__(self, dim, out_dim, patch_size, eps=1e-6):
        super().__init__()
        self.dim = dim
        self.out_dim = out_dim
        self.patch_size = patch_size
        self.eps = eps

        # layers
        out_dim = math.prod(patch_size) * out_dim
        self.norm = WanLayerNorm(dim, eps)
        self.head = nn.Linear(dim, out_dim)
        # modulation
        self.modulation = nn.Parameter(torch.randn(1, 2, dim) / dim**0.5)

    def forward(self, x, e):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            e(Tensor): Shape [B, C]
        """
        # assert e.dtype == torch.float32
        dtype = x.dtype

        latent_frames = e.shape[0]
        e = (self.modulation + e.unsqueeze(1)).chunk(2, dim=1)
        x = self.norm(x).to(dtype)
        x = reshape_latent(x , latent_frames)
        x *= (1 + e[1])
        x += e[0]
        x = restore_latent_shape(x)
        x= x.to(self.head.weight.dtype)
        x = self.head(x)
        return x


class MLPProj(torch.nn.Module):

    def __init__(self, in_dim, out_dim, flf_pos_emb=False):
        super().__init__()

        self.proj = torch.nn.Sequential(
            torch.nn.LayerNorm(in_dim), torch.nn.Linear(in_dim, in_dim),
            torch.nn.GELU(), torch.nn.Linear(in_dim, out_dim),
            torch.nn.LayerNorm(out_dim))
        
        if flf_pos_emb:  # NOTE: we only use this for `flf2v`
            FIRST_LAST_FRAME_CONTEXT_TOKEN_NUMBER = 257 * 2
            self.emb_pos = nn.Parameter(
                torch.zeros(1, FIRST_LAST_FRAME_CONTEXT_TOKEN_NUMBER, 1280))

    def forward(self, image_embeds):
        if hasattr(self, 'emb_pos'):
            bs, n, d = image_embeds.shape
            image_embeds = image_embeds.view(-1, 2 * n, d)
            image_embeds = image_embeds + self.emb_pos
        clip_extra_context_tokens = self.proj(image_embeds)
        return clip_extra_context_tokens

class WanModel(ModelMixin, ConfigMixin):
    def setup_chipmunk(self):
        # from chipmunk.util import LayerCounter
        # from chipmunk.modules import SparseDiffMlp, SparseDiffAttn
        seq_shape = (21, 45, 80)
        chipmunk_layers =[]
        for i in range(self.num_layers):
            layer_num, layer_counter = LayerCounter.build_for_layer(is_attn_sparse=True, is_mlp_sparse=False)            
            chipmunk_layers.append( SparseDiffAttn(layer_num, layer_counter))
        offload.shared_state["_chipmunk_layers"] = chipmunk_layers

        chipmunk_layers[0].initialize_static_mask(
            seq_shape=seq_shape,
            txt_len=0,
            local_heads_num=self.num_heads,
            device='cuda'
        )
        chipmunk_layers[0].layer_counter.reset()

    def release_chipmunk(self):
        offload.shared_state["_chipmunk_layers"] = None

    def preprocess_loras(self, model_type, sd):

        first = next(iter(sd), None)
        if first == None:
            return sd

        # if first.startswith("blocks."):
        #     new_sd = {}
        #     for k,v in sd.items():
        #         new_sd["diffusion_model." + k] = v
        #     sd = new_sd

        if first.startswith("lora_unet_"):
            new_sd = {}
            print("Converting Lora Safetensors format to Lora Diffusers format")
            alphas = {}
            repl_list = ["cross_attn", "self_attn", "ffn"]
            src_list = ["_" + k + "_" for k in repl_list]
            tgt_list = ["." + k + "." for k in repl_list]

            for k,v in sd.items():
                k = k.replace("lora_unet_blocks_","diffusion_model.blocks.")
                k = k.replace("lora_unet__blocks_","diffusion_model.blocks.")

                for s,t in zip(src_list, tgt_list):
                    k = k.replace(s,t)

                k = k.replace("lora_up","lora_B")
                k = k.replace("lora_down","lora_A")

                new_sd[k] = v

            sd = new_sd
        from wgp import test_class_i2v 
        if not test_class_i2v(model_type) or model_type in ["i2v_2_2"]:
            new_sd = {}
            # convert loras for i2v to t2v
            for k,v in sd.items():
                if  any(layer in k for layer in ["cross_attn.k_img", "cross_attn.v_img", "img_emb."]):
                    continue
                new_sd[k] = v
            sd = new_sd

        return sd    
    r"""
    Wan diffusion backbone supporting both text-to-video and image-to-video.
    """

    ignore_for_config = [
        'patch_size', 'cross_attn_norm', 'qk_norm', 'text_dim', 'window_size'
    ]
    _no_split_modules = ['WanAttentionBlock']

    @register_to_config
    def __init__(self,
                 vace_layers=None,
                 vace_in_dim=None,                 
                 model_type='t2v',
                 patch_size=(1, 2, 2),
                 text_len=512,
                 in_dim=16,
                 dim=2048,
                 ffn_dim=8192,
                 freq_dim=256,
                 text_dim=4096,
                 out_dim=16,
                 num_heads=16,
                 num_layers=32,
                 window_size=(-1, -1),
                 qk_norm=True,
                 cross_attn_norm=True,
                 eps=1e-6,
                 flf = False,
                 recammaster = False,
                 inject_sample_info = False,
                 fantasytalking_dim = 0,
                 multitalk_output_dim = 0,
                 audio_window=5,
                 intermediate_dim=512,
                 context_tokens=32,
                 vae_scale=4, # vae timedownsample scale
                 norm_input_visual=True,
                 norm_output_audio=True,
                 ):

        super().__init__()

        self.model_type = model_type

        self.patch_size = patch_size
        self.text_len = text_len
        self.in_dim = in_dim
        self.dim = dim
        self.ffn_dim = ffn_dim
        self.freq_dim = freq_dim
        self.text_dim = text_dim
        self.out_dim = out_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.cross_attn_norm = cross_attn_norm
        self.eps = eps
        self.num_frame_per_block = 1
        self.flag_causal_attention = False
        self.block_mask = None
        self.inject_sample_info = inject_sample_info

        self.norm_output_audio = norm_output_audio
        self.audio_window = audio_window
        self.intermediate_dim = intermediate_dim
        self.vae_scale = vae_scale

        multitalk = multitalk_output_dim > 0
        self.multitalk = multitalk

        # embeddings
        self.patch_embedding = nn.Conv3d(
            in_dim, dim, kernel_size=patch_size, stride=patch_size)
        self.text_embedding = nn.Sequential(
            nn.Linear(text_dim, dim), nn.GELU(approximate='tanh'),
            nn.Linear(dim, dim))

        if inject_sample_info:
            self.fps_embedding = nn.Embedding(2, dim)
            self.fps_projection = nn.Sequential(nn.Linear(dim, dim), nn.SiLU(), nn.Linear(dim, dim * 6))

        self.time_embedding = nn.Sequential(
            nn.Linear(freq_dim, dim), nn.SiLU(), nn.Linear(dim, dim))
        self.time_projection = nn.Sequential(nn.SiLU(), nn.Linear(dim, dim * 6))

        # blocks
        if vace_layers == None:
            cross_attn_type = 't2v_cross_attn' if model_type in ['t2v','i2v2_2', 'ti2v2_2'] else 'i2v_cross_attn'
            self.blocks = nn.ModuleList([
                WanAttentionBlock(cross_attn_type, dim, ffn_dim, num_heads,
                                window_size, qk_norm, cross_attn_norm, eps, block_no =i, output_dim=multitalk_output_dim, norm_input_visual=norm_input_visual)
                for i in range(num_layers)
            ])

        # head
        self.head = Head(dim, out_dim, patch_size, eps)

        # buffers (don't use register_buffer otherwise dtype will be changed in to())

        if model_type == 'i2v':
            self.img_emb = MLPProj(1280, dim, flf_pos_emb = flf)

        if multitalk :
            # init audio adapter
            self.audio_proj = AudioProjModel(
                        seq_len=audio_window,
                        seq_len_vf=audio_window+vae_scale-1,
                        intermediate_dim=intermediate_dim,
                        output_dim=multitalk_output_dim,
                        context_tokens=context_tokens,
                        norm_output_audio=norm_output_audio,
                    )

        # initialize weights
        self.init_weights()

        if vace_layers != None:            
            self.vace_layers = [i for i in range(0, self.num_layers, 2)] if vace_layers is None else vace_layers
            self.vace_in_dim = self.in_dim if vace_in_dim is None else vace_in_dim

            assert 0 in self.vace_layers
            self.vace_layers_mapping = {i: n for n, i in enumerate(self.vace_layers)}

            # blocks
            self.blocks = nn.ModuleList([
                WanAttentionBlock('t2v_cross_attn', self.dim, self.ffn_dim, self.num_heads, self.window_size, self.qk_norm,
                                    self.cross_attn_norm, self.eps, block_no =i,
                                    block_id=self.vace_layers_mapping[i] if i in self.vace_layers else None,
                                    output_dim=multitalk_output_dim,
                                    norm_input_visual=norm_input_visual,
                                    )
                for i in range(self.num_layers)
            ])

            # vace blocks
            self.vace_blocks = nn.ModuleList([
                VaceWanAttentionBlock('t2v_cross_attn', self.dim, self.ffn_dim, self.num_heads, self.window_size, self.qk_norm,
                                        self.cross_attn_norm, self.eps, block_id=i)
                for i in self.vace_layers
            ])

            # vace patch embeddings
            self.vace_patch_embedding = nn.Conv3d(
                self.vace_in_dim, self.dim, kernel_size=self.patch_size, stride=self.patch_size
            )
        if recammaster :
            dim=self.blocks[0].self_attn.q.weight.shape[0]
            for block in self.blocks:
                block.cam_encoder = nn.Linear(12, dim)
                block.projector = nn.Linear(dim, dim)
                block.cam_encoder.weight.data.zero_()
                block.cam_encoder.bias.data.zero_()
                block.projector.weight = nn.Parameter(torch.eye(dim))
                block.projector.bias = nn.Parameter(torch.zeros(dim))            

        if fantasytalking_dim > 0:
            from ..fantasytalking.model import WanCrossAttentionProcessor
            for block in self.blocks:
                block.cross_attn.processor = WanCrossAttentionProcessor(fantasytalking_dim, dim)


    def lock_layers_dtypes(self, hybrid_dtype = None, dtype = torch.float32):
        layer_list = [self.head, self.head.head, self.patch_embedding]
        target_dype= dtype
        
        layer_list2 = [ self.time_embedding, self.time_embedding[0], self.time_embedding[2], 
                    self.time_projection, self.time_projection[1]] #, self.text_embedding, self.text_embedding[0], self.text_embedding[2] ]

        for block in self.blocks:
            layer_list2 += [block.norm3]

        if hasattr(self, "audio_proj"):
            for block in self.blocks:
                layer_list2 += [block.norm_x]

        if hasattr(self, "fps_embedding"):
            layer_list2 += [self.fps_embedding, self.fps_projection, self.fps_projection[0], self.fps_projection[2]]

        if hasattr(self, "vace_patch_embedding"):
            layer_list2 += [self.vace_patch_embedding]
            layer_list2 += [self.vace_blocks[0].before_proj]
            for block in self.vace_blocks:
                layer_list2 += [block.after_proj, block.norm3]

        target_dype2 = hybrid_dtype if hybrid_dtype != None else dtype 

        # cam master
        if hasattr(self.blocks[0], "projector"):
            for block in self.blocks:
                layer_list2 += [block.projector]

        for current_layer_list, current_dtype in zip([layer_list, layer_list2], [target_dype, target_dype2]):
            for layer in current_layer_list:
                layer._lock_dtype = dtype

                if hasattr(layer, "weight") and layer.weight.dtype != current_dtype :
                    layer.weight.data = layer.weight.data.to(current_dtype)
                    if hasattr(layer, "bias"):
                        layer.bias.data = layer.bias.data.to(current_dtype)

        self._lock_dtype = dtype

    def compute_magcache_threshold(self, start_step, timesteps = None, speed_factor =0):
        skips_step_cache = self.cache
        def nearest_interp(src_array, target_length):
            src_length = len(src_array)
            if target_length == 1: return np.array([src_array[-1]])
            scale = (src_length - 1) / (target_length - 1)
            mapped_indices = np.round(np.arange(target_length) * scale).astype(int)
            return src_array[mapped_indices]
        num_inference_steps = len(timesteps)
        def_mag_ratios = np.array([1.0]*2+ skips_step_cache.def_mag_ratios)
        if len(def_mag_ratios) != num_inference_steps*2:
            mag_ratio_con = nearest_interp(def_mag_ratios[0::2], num_inference_steps)
            mag_ratio_ucon = nearest_interp(def_mag_ratios[1::2], num_inference_steps)
            interpolated_mag_ratios = np.concatenate([mag_ratio_con.reshape(-1, 1), mag_ratio_ucon.reshape(-1, 1)], axis=1).reshape(-1)
            skips_step_cache.mag_ratios = interpolated_mag_ratios
        else:
            skips_step_cache.mag_ratios = def_mag_ratios


        best_deltas = None
        best_threshold = 0.01
        best_diff = 1000
        best_signed_diff = 1000
        target_nb_steps= int(len(timesteps) / speed_factor)
        threshold = 0.01
        x_id_max = 1
        while threshold <= 0.6:
            nb_steps = 0
            diff = 1000
            accumulated_err, accumulated_steps, accumulated_ratio = [0] * x_id_max , [0] * x_id_max, [1.0] * x_id_max
            for i, t in enumerate(timesteps):
                if i<=start_step:
                    skip  = False
                    x_should_calc = [True] * x_id_max
                else:
                    x_should_calc = []
                    for cur_x_id in range(x_id_max):
                        cur_mag_ratio = skips_step_cache.mag_ratios[i * 2 + cur_x_id] # conditional and unconditional in one list
                        accumulated_ratio[cur_x_id] *= cur_mag_ratio # magnitude ratio between current step and the cached step
                        accumulated_steps[cur_x_id] += 1 # skip steps plus 1
                        cur_skip_err = np.abs(1-accumulated_ratio[cur_x_id]) # skip error of current steps
                        accumulated_err[cur_x_id] += cur_skip_err # accumulated error of multiple steps
                        if accumulated_err[cur_x_id]<threshold and accumulated_steps[cur_x_id]<=skips_step_cache.magcache_K:
                            skip  = True
                        else:
                            skip  = False
                            accumulated_err[cur_x_id], accumulated_steps[cur_x_id], accumulated_ratio[cur_x_id] = 0, 0, 1.0
                        x_should_calc.append(not skip)
                if not skip:
                    nb_steps += 1
                    signed_diff = target_nb_steps - nb_steps               
                    diff = abs(signed_diff)  
            if diff < best_diff:
                best_threshold = threshold
                best_diff = diff
                best_signed_diff = signed_diff
            elif diff > best_diff:
                break
            threshold += 0.01
        skips_step_cache.magcache_thresh = best_threshold
        print(f"Mag Cache, best threshold found:{best_threshold:0.2f} with gain x{len(timesteps)/(target_nb_steps - best_signed_diff):0.2f} for a target of x{speed_factor}")
        return best_threshold

    def compute_teacache_threshold(self, start_step, timesteps = None, speed_factor =0): 
        skips_step_cache = self.cache
        modulation_dtype = self.time_projection[1].weight.dtype
        rescale_func = np.poly1d(skips_step_cache.coefficients)
        e_list = []
        for t in timesteps:
            t = torch.stack([t])
            time_emb =  self.time_embedding( sinusoidal_embedding_1d(self.freq_dim, t.flatten()).to(modulation_dtype) )  # b, dim   
            e_list.append(time_emb)
        best_deltas = None
        best_threshold = 0.01
        best_diff = 1000
        best_signed_diff = 1000
        target_nb_steps= int(len(timesteps) / speed_factor)
        threshold = 0.01
        while threshold <= 0.6:
            accumulated_rel_l1_distance =0
            nb_steps = 0
            diff = 1000
            deltas = []
            for i, t in enumerate(timesteps):
                skip = False    
                if not (i<=start_step or i== len(timesteps)-1):
                    delta = abs(rescale_func(((e_list[i]-e_list[i-1]).abs().mean() / e_list[i-1].abs().mean()).cpu().item()))
                    # deltas.append(delta)
                    accumulated_rel_l1_distance += delta
                    if accumulated_rel_l1_distance < threshold:
                        skip = True
                        # deltas.append("SKIP")
                    else:
                        accumulated_rel_l1_distance = 0
                if not skip:
                    nb_steps += 1
                    signed_diff = target_nb_steps - nb_steps               
                    diff = abs(signed_diff)  
            if diff < best_diff:
                best_threshold = threshold
                best_deltas = deltas
                best_diff = diff
                best_signed_diff = signed_diff
            elif diff > best_diff:
                break
            threshold += 0.01
        skips_step_cache.rel_l1_thresh = best_threshold
        print(f"Tea Cache, best threshold found:{best_threshold:0.2f} with gain x{len(timesteps)/(target_nb_steps - best_signed_diff):0.2f} for a target of x{speed_factor}")
        # print(f"deltas:{best_deltas}")
        return best_threshold

    
    def forward(
        self,
        x,
        t,
        context,
        vace_context = None,
        vace_context_scale=[1.0],        
        clip_fea=None,
        y=None,
        freqs = None,
        pipeline = None,
        current_step = 0,
        x_id= 0,
        max_steps = 0, 
        slg_layers=None,
        callback = None,
        cam_emb: torch.Tensor = None,
        fps = None,
        causal_block_size = 1,
        causal_attention = False,
        audio_proj=None,
        audio_context_lens=None,
        audio_scale=None,
        multitalk_audio = None,
        multitalk_masks = None,
        ref_images_count = 0,    

    ):
        # patch_dtype =  self.patch_embedding.weight.dtype
        modulation_dtype = self.time_projection[1].weight.dtype

        if self.model_type == 'i2v':
            assert clip_fea is not None and y is not None
        # params
        device = self.patch_embedding.weight.device
        if torch.is_tensor(freqs) and freqs.device != device:
            freqs = freqs.to(device)

        chipmunk = offload.shared_state.get("_chipmunk", False) 
        if chipmunk:
            # from chipmunk.ops.voxel import voxel_chunk_no_padding, reverse_voxel_chunk_no_padding
            voxel_shape = (4, 6, 8)

        x_list = x
        joint_pass = len(x_list) > 1
        is_source_x = [ x.data_ptr() == x_list[0].data_ptr() and i > 0 for i, x in enumerate(x_list) ]
        last_x_idx  = 0
        for i, (is_source, x) in enumerate(zip(is_source_x, x_list)):
            if is_source:
                x_list[i] = x_list[0].clone()
                last_x_idx = i
            else:
                # image source
                bz = len(x)
                if y is not None:
                    y = y.unsqueeze(0)        
                    if bz > 1: y = y.expand(bz, -1, -1, -1, -1)
                    x = torch.cat([x, y], dim=1)
                # embeddings
                # x = self.patch_embedding(x.unsqueeze(0)).to(modulation_dtype)
                x = self.patch_embedding(x).to(modulation_dtype)
                grid_sizes = x.shape[2:]
                if chipmunk:
                    x = x.unsqueeze(-1)
                    x_og_shape = x.shape
                    x = voxel_chunk_no_padding(x, voxel_shape).squeeze(-1).transpose(1, 2)
                else:
                    x = x.flatten(2).transpose(1, 2)
                x_list[i] = x
        x, y = None, None


        block_mask = None
        if causal_attention and causal_block_size > 0 and False: # NEVER WORKED
            frame_num = grid_sizes[0]
            height = grid_sizes[1]
            width = grid_sizes[2]
            block_num = frame_num // causal_block_size
            range_tensor = torch.arange(block_num).view(-1, 1)
            range_tensor = range_tensor.repeat(1, causal_block_size).flatten()
            causal_mask = range_tensor.unsqueeze(0) <= range_tensor.unsqueeze(1)  # f, f
            causal_mask = causal_mask.view(frame_num, 1, 1, frame_num, 1, 1).to(x[0].device)
            causal_mask = causal_mask.repeat(1, height, width, 1, height, width)
            causal_mask = causal_mask.reshape(frame_num * height * width, frame_num * height * width)
            block_mask = causal_mask.unsqueeze(0).unsqueeze(0)
            del causal_mask

        offload.shared_state["embed_sizes"] = grid_sizes 
        offload.shared_state["step_no"] = current_step 
        offload.shared_state["max_steps"] = max_steps

        _flag_df = t.dim() == 2

        e = self.time_embedding(
            sinusoidal_embedding_1d(self.freq_dim, t.flatten()).to(modulation_dtype)  # self.patch_embedding.weight.dtype)
        )  # b, dim        
        e0 = self.time_projection(e).unflatten(1, (6, self.dim)).to(e.dtype)

        if self.inject_sample_info and fps!=None:
            fps = torch.tensor(fps, dtype=torch.long, device=device)

            fps_emb = self.fps_embedding(fps).to(e.dtype) 
            if _flag_df:
                e0 = e0 + self.fps_projection(fps_emb).unflatten(1, (6, self.dim)).repeat(t.shape[1], 1, 1)
            else:
                e0 = e0 + self.fps_projection(fps_emb).unflatten(1, (6, self.dim))

        # context
        context = [self.text_embedding( u ) for u in context  ] 
        
        if clip_fea is not None:
            context_clip = self.img_emb(clip_fea)  # bs x 257 x dim
            context_list = []
            for one_context in context: 
                if len(one_context) != len(context_clip):
                    context_list.append( torch.cat( [context_clip.repeat(len(one_context), 1, 1), one_context ], dim=1 ))
                else:
                    context_list.append( torch.cat( [context_clip, one_context ], dim=1 ))
        else:
            context_list = context

        if multitalk_audio != None:
            multitalk_audio_list = []
            for audio in multitalk_audio:
                audio = self.audio_proj(*audio) 
                audio = torch.concat(audio.split(1), dim=2).to(context[0])
                multitalk_audio_list.append(audio)
            audio = None
        else:
            multitalk_audio_list = [None] * len(x_list)

        if multitalk_masks != None:
            multitalk_masks_list = multitalk_masks
        else:
            multitalk_masks_list = [None] * len(x_list)

        if audio_scale != None: 
            audio_scale_list = audio_scale
        else:
            audio_scale_list = [None] * len(x_list)

            # arguments

        kwargs = dict(
            grid_sizes=grid_sizes,
            freqs=freqs,
            cam_emb = cam_emb,
            block_mask = block_mask,
            audio_proj=audio_proj,
            audio_context_lens=audio_context_lens,
            ref_images_count=ref_images_count,
            )

        if vace_context == None:
            hints_list = [None ] *len(x_list)
        else:
            # Vace embeddings
            c = [self.vace_patch_embedding(u.to(self.vace_patch_embedding.weight.dtype).unsqueeze(0)) for u in vace_context]
            c = [u.flatten(2).transpose(1, 2) for u in c]
            kwargs['context_scale'] = vace_context_scale
            hints_list = [ [ [sub_c] for sub_c in c] for _ in range(len(x_list)) ] 
            del c
        should_calc = True
        x_should_calc = None
        skips_steps_cache = self.cache
        if skips_steps_cache != None: 
            if skips_steps_cache.cache_type == "mag":
                if current_step <= skips_steps_cache.start_step:
                    should_calc = True
                elif skips_steps_cache.one_for_all and x_id != 0: # not joint pass, not main pas, one for all
                    assert len(x_list) == 1
                    should_calc = skips_steps_cache.should_calc
                else:
                    x_should_calc = []
                    for i in range(1 if skips_steps_cache.one_for_all else len(x_list)):
                        cur_x_id = i if joint_pass else x_id  
                        cur_mag_ratio = skips_steps_cache.mag_ratios[current_step * 2 + cur_x_id] # conditional and unconditional in one list
                        skips_steps_cache.accumulated_ratio[cur_x_id] *= cur_mag_ratio # magnitude ratio between current step and the cached step
                        skips_steps_cache.accumulated_steps[cur_x_id] += 1 # skip steps plus 1
                        cur_skip_err = np.abs(1-skips_steps_cache.accumulated_ratio[cur_x_id]) # skip error of current steps
                        skips_steps_cache.accumulated_err[cur_x_id] += cur_skip_err # accumulated error of multiple steps
                        if skips_steps_cache.accumulated_err[cur_x_id]<skips_steps_cache.magcache_thresh and skips_steps_cache.accumulated_steps[cur_x_id]<=skips_steps_cache.magcache_K:
                            skip_forward = True
                            if i == 0 and x_id == 0: skips_steps_cache.skipped_steps += 1
                            # print(f"skip: step={current_step} for x_id={cur_x_id}, accum error {skips_step_cache.accumulated_err[cur_x_id]}")
                        else:
                            skip_forward = False
                            skips_steps_cache.accumulated_err[cur_x_id], skips_steps_cache.accumulated_steps[cur_x_id], skips_steps_cache.accumulated_ratio[cur_x_id] = 0, 0, 1.0
                        x_should_calc.append(not skip_forward)
                    if skips_steps_cache.one_for_all:
                        should_calc = skips_steps_cache.should_calc = x_should_calc[0] 
                        x_should_calc = None
            else:
                if x_id != 0:
                    should_calc = skips_steps_cache.should_calc
                else:
                    if current_step <= skips_steps_cache.start_step or current_step == skips_steps_cache.num_steps-1:
                        should_calc = True
                        skips_steps_cache.accumulated_rel_l1_distance = 0
                    else:
                        rescale_func = np.poly1d(skips_steps_cache.coefficients)
                        delta = abs(rescale_func(((e-skips_steps_cache.previous_modulated_input).abs().mean() / skips_steps_cache.previous_modulated_input.abs().mean()).cpu().item()))
                        skips_steps_cache.accumulated_rel_l1_distance += delta
                        if skips_steps_cache.accumulated_rel_l1_distance < skips_steps_cache.rel_l1_thresh:
                            should_calc = False
                            skips_steps_cache.skipped_steps += 1
                            # print(f"Teacache Skipped Step no {current_step} ({skips_step_cache.cache_skipped_steps}/{current_step}), delta={delta}" )
                        else:
                            should_calc = True
                            skips_steps_cache.accumulated_rel_l1_distance = 0
                    skips_steps_cache.previous_modulated_input = e 
                    skips_steps_cache.should_calc = should_calc

        if x_should_calc  == None: x_should_calc = [should_calc] * len(x_list) 

        if joint_pass:
            for i, x in enumerate(x_list):
                if not x_should_calc[i]: x += skips_steps_cache.previous_residual[i]
        elif not x_should_calc[0]:
            x = x_list[0]
            x += skips_steps_cache.previous_residual[x_id]
        x = None

        if skips_steps_cache != None:
            if skips_steps_cache.previous_residual == None: skips_steps_cache.previous_residual = [ None ] * len(x_list)
    
            if joint_pass:
                for i, should_calc in enumerate(x_should_calc):
                    if should_calc: skips_steps_cache.previous_residual[i] = None
            elif x_should_calc[0]:
                skips_steps_cache.previous_residual[x_id] = None
            ori_hidden_states = [ None ] * len(x_list)
            if all(x_should_calc):
                ori_hidden_states[0] = x_list[0].clone()
                for i in range(1, len(x_list)):
                    ori_hidden_states[i] = ori_hidden_states[0] if is_source_x[i] else x_list[i].clone()
            else:
                for i in range(len(x_list)):
                    if x_should_calc[i]: ori_hidden_states[i] = x_list[i].clone()

        if any(x_should_calc):
            for block_idx, block in enumerate(self.blocks):
                offload.shared_state["layer"] = block_idx
                if callback != None:
                    callback(-1, None, False, True)
                if pipeline._interrupt:
                    return [None] * len(x_list)

                # if (x_id != 0 or joint_pass) and slg_layers is not None and block_idx in slg_layers:
                #     if not joint_pass or not x_should_calc[0]:

                if slg_layers is not None and block_idx in slg_layers:
                    if x_id != 0 or not x_should_calc[0]:
                        continue
                    x_list[0] = block(x_list[0], context = context_list[0], audio_scale= audio_scale_list[0], e= e0, **kwargs)
                else:
                    for i, (x, context, hints, audio_scale, multitalk_audio, multitalk_masks, should_calc) in enumerate(zip(x_list, context_list, hints_list, audio_scale_list, multitalk_audio_list, multitalk_masks_list, x_should_calc)):
                        if should_calc:
                            x_list[i] = block(x, context = context, hints= hints, audio_scale= audio_scale, multitalk_audio = multitalk_audio, multitalk_masks =multitalk_masks, e= e0, **kwargs)
                            del x
                    context = hints = audio_embedding  = None

        if skips_steps_cache != None:
            if joint_pass:
                if all(x_should_calc):                        
                    for i, (x, ori, is_source) in enumerate(zip(x_list, ori_hidden_states, is_source_x)) :
                        if i == 0 or is_source and i != last_x_idx  :
                            skips_steps_cache.previous_residual[i] = torch.sub(x, ori) 
                        else:
                            skips_steps_cache.previous_residual[i] = ori
                            torch.sub(x, ori, out=skips_steps_cache.previous_residual[i]) 
                        ori_hidden_states[i] = None
                else:
                    for i, (x, ori, is_source, should_calc) in enumerate(zip(x_list, ori_hidden_states, is_source_x, x_should_calc)) :
                        if should_calc:
                            skips_steps_cache.previous_residual[i] = ori
                            torch.sub(x, ori, out=skips_steps_cache.previous_residual[i]) 
                        ori_hidden_states[i] = None
                x , ori = None, None
            elif x_should_calc[0]:
                residual = ori_hidden_states[0] # just to have a readable code
                torch.sub(x_list[0], ori_hidden_states[0], out=residual)
                skips_steps_cache.previous_residual[x_id] = residual
            residual, ori_hidden_states = None, None

        for i, x in enumerate(x_list):
            if chipmunk:
                x = reverse_voxel_chunk_no_padding(x.transpose(1, 2).unsqueeze(-1), x_og_shape, voxel_shape).squeeze(-1)
                x = x.flatten(2).transpose(1, 2)

            # head
            x = self.head(x, e)

            # unpatchify
            x_list[i] = self.unpatchify(x, grid_sizes)
            del x

        return [x.float() for x in x_list]

    def unpatchify(self, x, grid_sizes):
        r"""
        Reconstruct video tensors from patch embeddings.

        Args:
            x (List[Tensor]):
                List of patchified features, each with shape [L, C_out * prod(patch_size)]
            grid_sizes (Tensor):
                Original spatial-temporal grid dimensions before patching,
                    shape [B, 3] (3 dimensions correspond to F_patches, H_patches, W_patches)

        Returns:
            List[Tensor]:
                Reconstructed video tensors with shape [C_out, F, H / 8, W / 8]
        """

        c = self.out_dim
        out = []
        for u in x:
            u = u[:math.prod(grid_sizes)].view(*grid_sizes, *self.patch_size, c)
            u = torch.einsum('fhwpqrc->cfphqwr', u)
            u = u.reshape(c, *[i * j for i, j in zip(grid_sizes, self.patch_size)])
            out.append(u)
        if len(x) == 1:
            return out[0].unsqueeze(0)
        else:
            return torch.stack(out, 0)

    def init_weights(self):
        r"""
        Initialize model parameters using Xavier initialization.
        """

        # basic init
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

        # init embeddings
        nn.init.xavier_uniform_(self.patch_embedding.weight.flatten(1))
        for m in self.text_embedding.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=.02)
        for m in self.time_embedding.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=.02)

        # init output layer
        nn.init.zeros_(self.head.head.weight)
