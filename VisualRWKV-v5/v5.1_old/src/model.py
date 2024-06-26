########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import os, math, gc, importlib
import torch
# torch._C._jit_set_profiling_executor(True)
# torch._C._jit_set_profiling_mode(True)
import torch.nn as nn
from torch.nn import functional as F
import pytorch_lightning as pl
from pytorch_lightning.utilities import rank_zero_info, rank_zero_only
from pytorch_lightning.strategies import DeepSpeedStrategy
from transformers import CLIPVisionModel
if importlib.util.find_spec('deepspeed'):
    import deepspeed
    from deepspeed.ops.adam import DeepSpeedCPUAdam, FusedAdam

# from deepspeed.runtime.fp16.onebit.zoadam import ZeroOneAdam
from .dataset import IGNORE_INDEX, IMAGE_TOKEN_INDEX, PAD_TOKEN_INDEX

def __nop(ob):
    return ob


MyModule = nn.Module
MyFunction = __nop
if os.environ["RWKV_JIT_ON"] == "1":
    MyModule = torch.jit.ScriptModule
    MyFunction = torch.jit.script_method


########################################################################################################
# CUDA Kernel
########################################################################################################

from torch.utils.cpp_extension import load

HEAD_SIZE = int(os.environ["RWKV_HEAD_SIZE_A"])
wkv5_cuda = load(name="wkv5", sources=["cuda/wkv5_op.cpp", f"cuda/wkv5_cuda.cu"],
                verbose=True, extra_cuda_cflags=["-res-usage", "--use_fast_math", "-O3", "-Xptxas -O3", "--extra-device-vectorization", f"-D_N_={HEAD_SIZE}"])
    
class WKV_5(torch.autograd.Function):
    @staticmethod
    def forward(ctx, B, T, C, H, r, k, v, w, u):
        with torch.no_grad():
            assert r.dtype == torch.bfloat16
            assert k.dtype == torch.bfloat16
            assert v.dtype == torch.bfloat16
            assert w.dtype == torch.bfloat16
            assert u.dtype == torch.bfloat16
            assert HEAD_SIZE == C // H
            ctx.B = B
            ctx.T = T
            ctx.C = C
            ctx.H = H
            assert r.is_contiguous()
            assert k.is_contiguous()
            assert v.is_contiguous()
            assert w.is_contiguous()
            assert u.is_contiguous()
            ew = (-torch.exp(w.float())).contiguous()
            eew = (torch.exp(ew)).contiguous()
            ctx.save_for_backward(r, k, v, eew, ew, u)
            y = torch.empty((B, T, C), device=r.device, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            wkv5_cuda.forward(B, T, C, H, r, k, v, eew, u, y)
            return y

    @staticmethod
    def backward(ctx, gy):
        with torch.no_grad():
            assert gy.dtype == torch.bfloat16
            B = ctx.B
            T = ctx.T
            C = ctx.C
            H = ctx.H
            assert gy.is_contiguous()
            r, k, v, eew, ew, u = ctx.saved_tensors
            gr = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gk = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gv = torch.empty((B, T, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gw = torch.empty((B, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            gu = torch.empty((B, C), device=gy.device, requires_grad=False, dtype=torch.bfloat16, memory_format=torch.contiguous_format) # .uniform_(-1, 1)
            wkv5_cuda.backward(B, T, C, H, r, k, v, eew, ew, u, gy, gr, gk, gv, gw, gu)
            gw = torch.sum(gw, 0).view(H, C//H)
            gu = torch.sum(gu, 0).view(H, C//H)
            return (None, None, None, None, gr, gk, gv, gw, gu)

def RUN_CUDA_RWKV5(B, T, C, H, r, k, v, w, u):
    return WKV_5.apply(B, T, C, H, r, k, v, w, u)

########################################################################################################

class RWKV_TimeMix_RWKV5(MyModule):
    def __init__(self, args, layer_id):
        super().__init__()
        self.args = args
        self.layer_id = layer_id

        self.head_size = args.head_size_a
        assert HEAD_SIZE == self.head_size # change HEAD_SIZE to match args.head_size_a
        self.n_head = args.dim_att // self.head_size
        assert args.dim_att % self.n_head == 0
        self.head_size_divisor = args.head_size_divisor

        with torch.no_grad():
            ratio_0_to_1 = layer_id / (args.n_layer - 1)  # 0 to 1
            ratio_1_to_almost0 = 1.0 - (layer_id / args.n_layer)  # 1 to ~0
            ddd = torch.ones(1, 1, args.n_embd)
            for i in range(args.n_embd):
                ddd[0, 0, i] = i / args.n_embd

            # fancy time_mix
            self.time_mix_k = nn.Parameter(torch.pow(ddd, ratio_1_to_almost0))
            self.time_mix_v = nn.Parameter(torch.pow(ddd, ratio_1_to_almost0) + 0.3 * ratio_0_to_1)
            self.time_mix_r = nn.Parameter(torch.pow(ddd, 0.5 * ratio_1_to_almost0))
            self.time_mix_g = nn.Parameter(torch.pow(ddd, 0.5 * ratio_1_to_almost0))

            # fancy time_decay
            decay_speed = torch.ones(args.dim_att)
            for n in range(args.dim_att):
                decay_speed[n] = -6 + 5 * (n / (args.dim_att - 1)) ** (0.7 + 1.3 * ratio_0_to_1)
            self.time_decay = nn.Parameter(decay_speed.reshape(self.n_head, self.head_size))
            # print(layer_id, self.time_decay.flatten()[:3].cpu().numpy(), '...', self.time_decay.flatten()[-3:].cpu().numpy())

            tmp = torch.zeros(args.dim_att)
            for n in range(args.dim_att):
                zigzag = ((n + 1) % 3 - 1) * 0.1
                tmp[n] = ratio_0_to_1 * (1 - (n / (args.dim_att - 1))) + zigzag

            self.time_faaaa = nn.Parameter(tmp.reshape(self.n_head, self.head_size))

        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))
        self.receptance = nn.Linear(args.n_embd, args.dim_att, bias=False)
        self.key = nn.Linear(args.n_embd, args.dim_att, bias=False)

        self.value = nn.Linear(args.n_embd, args.dim_att, bias=False)
        self.output = nn.Linear(args.dim_att, args.n_embd, bias=False)
        self.gate = nn.Linear(args.n_embd, args.dim_att, bias=False)
        self.ln_x = nn.GroupNorm(self.n_head, args.dim_att)

    @MyFunction
    def jit_func(self, x):
        B, T, C = x.size()

        xx = self.time_shift(x) # Mix x with the previous timestep to produce xk, xv, xr
        xk = x * self.time_mix_k + xx * (1 - self.time_mix_k)
        xv = x * self.time_mix_v + xx * (1 - self.time_mix_v)
        xr = x * self.time_mix_r + xx * (1 - self.time_mix_r)
        xg = x * self.time_mix_g + xx * (1 - self.time_mix_g)

        r = self.receptance(xr)
        k = self.key(xk)
        v = self.value(xv)
        g = F.silu(self.gate(xg))

        return r, k, v, g

    @MyFunction
    def jit_func_2(self, x, g):
        B, T, C = x.size()
        x = x.view(B * T, C)
        
        x = self.ln_x(x / self.head_size_divisor).view(B, T, C)
        x = self.output(x * g)
        return x

    def forward(self, x):
        B, T, C = x.size()
        H = self.n_head

        r, k, v, g = self.jit_func(x)

        x = RUN_CUDA_RWKV5(B, T, C, H, r, k, v, w=self.time_decay, u=self.time_faaaa)

        return self.jit_func_2(x, g)

########################################################################################################

class RWKV_ChannelMix(MyModule):
    def __init__(self, args, layer_id):
        super().__init__()
        self.args = args
        self.layer_id = layer_id
        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))

        with torch.no_grad():  # fancy init of time_mix
            ratio_1_to_almost0 = 1.0 - (layer_id / args.n_layer)  # 1 to ~0
            ddd = torch.ones(1, 1, args.n_embd)
            for i in range(args.n_embd):
                ddd[0, 0, i] = i / args.n_embd
            self.time_mix_k = nn.Parameter(torch.pow(ddd, ratio_1_to_almost0))
            self.time_mix_r = nn.Parameter(torch.pow(ddd, ratio_1_to_almost0))
        
        self.key = nn.Linear(args.n_embd, args.dim_ffn, bias=False)
        self.receptance = nn.Linear(args.n_embd, args.n_embd, bias=False)
        self.value = nn.Linear(args.dim_ffn, args.n_embd, bias=False)

    @MyFunction
    def forward(self, x):
        xx = self.time_shift(x)
        xk = x * self.time_mix_k + xx * (1 - self.time_mix_k)
        xr = x * self.time_mix_r + xx * (1 - self.time_mix_r)
        k = self.key(xk)
        k = torch.relu(k) ** 2
        kv = self.value(k)
        return torch.sigmoid(self.receptance(xr)) * kv

########################################################################################################
# The RWKV Model with our blocks
########################################################################################################


class Block(nn.Module):
    def __init__(self, args, layer_id):
        super().__init__()
        self.args = args
        self.layer_id = layer_id

        self.ln1 = nn.LayerNorm(args.n_embd)
        self.ln2 = nn.LayerNorm(args.n_embd)

        if self.layer_id == 0:
            self.ln0 = nn.LayerNorm(args.n_embd)

        if self.layer_id == 0 and self.args.pre_ffn > 0:
            self.ffnPre = RWKV_ChannelMix(args, 0)
        else:
            self.att = RWKV_TimeMix_RWKV5(args, layer_id)

        self.ffn = RWKV_ChannelMix(args, layer_id)

        if args.dropout > 0:
            self.drop0 = nn.Dropout(p = args.dropout)
            self.drop1 = nn.Dropout(p = args.dropout)
        
    def forward(self, x):
        args = self.args
        B, T, C = x.size()
        if self.layer_id == 0:
            x = self.ln0(x)

        if self.args.dropout == 0:
            if self.layer_id == 0 and args.pre_ffn > 0:
                x = x + self.ffnPre(self.ln1(x))
            else:
                x = x + self.att(self.ln1(x))
            x = x + self.ffn(self.ln2(x))
        else:
            if self.layer_id == 0 and args.pre_ffn > 0:
                x = self.drop0(x + self.ffnPre(self.ln1(x)))
            else:
                x = self.drop0(x + self.att(self.ln1(x)))
            x = self.drop1(x + self.ffn(self.ln2(x)))

        return x


class L2Wrap(torch.autograd.Function):
    @staticmethod
    def forward(ctx, loss, y):
        ctx.save_for_backward(y)
        return loss

    @staticmethod
    def backward(ctx, grad_output):
        y = ctx.saved_tensors[0]
        # to encourage the logits to be close to 0
        factor = 1e-4 / (y.shape[0] * y.shape[1])
        maxx, ids = torch.max(y, -1, keepdim=True)
        gy = torch.zeros_like(y)
        gy.scatter_(-1, ids, maxx * factor)
        return (grad_output, gy)


class RWKV(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.emb = nn.Embedding(args.vocab_size, args.n_embd)
        self.blocks = nn.ModuleList([Block(args, i) for i in range(args.n_layer)])
        self.ln_out = nn.LayerNorm(args.n_embd)
        self.head = nn.Linear(args.n_embd, args.vocab_size, bias=False)

        if args.dropout > 0:
            self.drop0 = nn.Dropout(p = args.dropout)

    def configure_optimizers(self):
        trainable_params = [p for p in self.parameters() if p.requires_grad]
        optim_groups = [{"params": trainable_params, "weight_decay": self.args.weight_decay}]
        if self.deepspeed_offload:
            return DeepSpeedCPUAdam(optim_groups, lr=self.args.lr_init, betas=self.args.betas, eps=self.args.adam_eps, bias_correction=True, adamw_mode=True, amsgrad=False)
        return FusedAdam(optim_groups, lr=self.args.lr_init, betas=self.args.betas, eps=self.args.adam_eps, bias_correction=True, adam_w_mode=True, amsgrad=False)

    @property
    def deepspeed_offload(self) -> bool:
        strategy = self.trainer.strategy
        if isinstance(strategy, DeepSpeedStrategy):
            cfg = strategy.config["zero_optimization"]
            return cfg.get("offload_optimizer") or cfg.get("offload_param")
        return False

    def forward(self, x):
        args = self.args
        # B, T, D = x.size()
        # assert T <= args.ctx_len, "Cannot forward, model ctx_len is exhausted."

        if args.dropout > 0:
            x = self.drop0(x)

        for block in self.blocks:
            if args.grad_cp == 1:
                x = deepspeed.checkpointing.checkpoint(block, x)
            else:
                x = block(x)

        x = self.ln_out(x)

        x = self.head(x)

        return x

    def training_step(self, batch, batch_idx):
        idx, targets = batch
        logits = self(idx)
        loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        return L2Wrap.apply(loss, logits)

    def training_step_end(self, batch_parts):
        if pl.__version__[0]!='2':
            all = self.all_gather(batch_parts)
            if self.trainer.is_global_zero:
                self.trainer.my_loss_all = all


class VisualRWKV(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.rwkv = RWKV(args)
        if len(args.load_model) > 0:
            self.load_rwkv_from_pretrained(args.load_model)
        self.vit = CLIPVisionModel.from_pretrained(args.vision_tower_name)
        self.vit.requires_grad_(False)
        self.proj = nn.Linear(self.vit.config.hidden_size, args.n_embd, bias=False)
        self.align = ContrastiveAlignment(self.proj, args.queue_size, 
                                          args.ctx_len, args.vision_ctx_len, 
                                          args.n_embd, self.vit.config.hidden_size,
                                          reduction=args.constraive_reduction)

    def load_rwkv_from_pretrained(self, path):
        self.rwkv.load_state_dict(torch.load(path, map_location="cpu"))
        rank_zero_info(f"Loaded pretrained RWKV from {path}")

    @property
    def deepspeed_offload(self) -> bool:
        strategy = self.trainer.strategy
        if isinstance(strategy, DeepSpeedStrategy):
            cfg = strategy.config["zero_optimization"]
            return cfg.get("offload_optimizer") or cfg.get("offload_param")
        return False
    
    def freeze_rwkv(self, num_layers_to_freeze=0):
        # freeze all layers including embedding and lm head
        if num_layers_to_freeze == self.args.n_layer:
            self.rwkv.requires_grad_(False)
        # otherwise, freeze only the first num_layers_to_freeze layers
        for i, block in enumerate(self.rwkv.blocks):
            if i < num_layers_to_freeze:
                for p in block.parameters():
                    p.requires_grad_(False)
            else:
                for p in block.parameters():
                    p.requires_grad_(True)
        # freeze embedding if num_layers_to_freeze != 0
        if num_layers_to_freeze == 0:
            self.rwkv.emb.requires_grad_(True)
        else:
            self.rwkv.emb.requires_grad_(False)

    def freeze_proj(self):
        self.proj.requires_grad_(False)

    def configure_optimizers(self):
        trainable_params = [p for p in self.parameters() if p.requires_grad]
        name_of_trainable_params = [n for n, p in self.named_parameters() if p.requires_grad]
        rank_zero_info(f"Name of trainable parameters in optimizers: {name_of_trainable_params}")
        rank_zero_info(f"Number of trainable parameters in optimizers: {len(trainable_params)}")
        optim_groups = [{"params": trainable_params, "weight_decay": self.args.weight_decay}]
        if self.deepspeed_offload:
            return DeepSpeedCPUAdam(optim_groups, lr=self.args.lr_init, betas=self.args.betas, eps=self.args.adam_eps, bias_correction=True, adamw_mode=True, amsgrad=False)
        return FusedAdam(optim_groups, lr=self.args.lr_init, betas=self.args.betas, eps=self.args.adam_eps, bias_correction=True, adam_w_mode=True, amsgrad=False)

    def forward(self, samples):
        # encode images
        image_features  = self.encode_images(samples["images"], do_proj=False)
        image_features_projected = self.proj(image_features)
        # prepare embedding
        x, targets = self.preparing_embedding(samples, image_features_projected)
        logits = self.rwkv(x)
        # compute constraive loss
        # replace IMAGE_TOKEN_INDEX with PAD_TOKEN_INDEX
        input_ids = samples["input_ids"].clone()
        input_ids[input_ids == IMAGE_TOKEN_INDEX] = PAD_TOKEN_INDEX
        text_masks = (input_ids != PAD_TOKEN_INDEX)
        text_embeds = self.rwkv.emb(input_ids)
        constraive_loss = self.align(text_embeds=text_embeds, vision_embeds=image_features, text_masks=text_masks)
        return logits, targets, constraive_loss
    
    def training_step(self, batch, batch_idx):
        logits, targets, constraive_loss = self(batch)
        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = targets[..., 1:].contiguous()
        loss = F.cross_entropy(shift_logits.view(-1, shift_logits.size(-1)),
                               shift_labels.view(-1))
        constraive_loss = constraive_loss * self.args.constraive_loss_weight
        # record loss
        if not hasattr(self.trainer, "lm_loss_all"):
            self.trainer.lm_loss_all = []
        if not hasattr(self.trainer, "constraive_loss_all"):
            self.trainer.constraive_loss_all = []
        self.trainer.lm_loss_all.append(loss.item())
        self.trainer.constraive_loss_all.append(constraive_loss.item())
        self.trainer.lm_loss_all = self.trainer.lm_loss_all[-self.args.epoch_steps:]
        self.trainer.constraive_loss_all = self.trainer.constraive_loss_all[-self.args.epoch_steps:]
        # compute total loss
        loss = loss + constraive_loss
        return L2Wrap.apply(loss, logits)
    
    def training_step_end(self, batch_parts):
        if pl.__version__[0]!='2':
            all = self.all_gather(batch_parts)
            if self.trainer.is_global_zero:
                self.trainer.my_loss_all = all
    
    def encode_images(self, images, do_proj=True):
        B, N, C, H, W = images.shape
        images = images.view(B*N, C, H, W)
        image_features = self.vit(images).last_hidden_state
        L, D = image_features.shape[1], image_features.shape[2]
        # rerange [B*N, L, D] -> [B, N, L, D]
        image_features = image_features.view(B, N, L, D)[:, 0, :, :]
        image_features = self.grid_pooling(image_features)
        if do_proj:
            return self.proj(image_features)
        return image_features
    
    def grid_pooling(self, image_features):
        if self.args.grid_size == -1: # no grid pooling
            return image_features
        if self.args.grid_size == 0: # take cls token
            return image_features[:, 0:1, :]
        if self.args.grid_size == 1: # global avg pooling
            return image_features.mean(dim=1, keepdim=True)
        cls_features = image_features[:, 0:1, :]
        image_features = image_features[:, 1:, :] #drop cls token
        B, L, D = image_features.shape
        H_or_W = int(L**0.5)
        image_features = image_features.view(B, H_or_W, H_or_W, D)
        grid_stride = H_or_W // self.args.grid_size
        assert grid_stride * self.args.grid_size == H_or_W
        image_features = F.avg_pool2d(image_features.permute(0, 3, 1, 2), 
                                      padding=0,
                                      kernel_size=grid_stride, 
                                      stride=grid_stride)
        image_features = image_features.permute(0, 2, 3, 1).view(B, -1, D)
        return torch.cat((cls_features, image_features), dim=1)
   
    def preparing_embedding(self, samples, image_features, truncate=True):
        device, label_dtype = samples["labels"].device, samples["labels"].dtype
        emb_dtype = samples["images"].dtype
        ### prepare input token
        new_input_embeds = []
        new_labels = []
        for idx, cur_input_ids in enumerate(samples["input_ids"]):
            num_images = (cur_input_ids == IMAGE_TOKEN_INDEX).sum()
            if num_images == 0: # no image in this sample
                new_input_embeds.append(self.rwkv.emb(cur_input_ids))
                new_labels.append(samples["labels"][idx])
            elif num_images == 1: # only one image in this sample
                image_token_indice = torch.where(cur_input_ids == IMAGE_TOKEN_INDEX)[0][0]
                cur_labels = samples["labels"][idx]
                # first text part
                cur_new_input_embeds = [self.rwkv.emb(cur_input_ids[:image_token_indice])]
                cur_new_labels = [cur_labels[:image_token_indice]]
                # image part
                cur_image_features = image_features[idx]
                cur_new_input_embeds.append(cur_image_features)
                cur_new_labels.append(torch.full((cur_image_features.shape[0],), IGNORE_INDEX, device=device, dtype=label_dtype))
                # last text part
                cur_new_input_embeds.append(self.rwkv.emb(cur_input_ids[image_token_indice+1:]))
                cur_new_labels.append(cur_labels[image_token_indice+1:])
                # concat them
                cur_new_input_embeds = torch.cat(cur_new_input_embeds)
                cur_new_labels = torch.cat(cur_new_labels)

                new_input_embeds.append(cur_new_input_embeds)
                new_labels.append(cur_new_labels)
            else:
                raise ValueError(f"Too many images in one sample: {num_images}, should be 0 or 1.")
        # Truncate sequences to max length as image embeddings can make the sequence longer
        if truncate:
            new_input_embeds = [x[:self.args.ctx_len] for x in new_input_embeds]
            new_labels = [x[:self.args.ctx_len] for x in new_labels]
        # Combine them
        max_len = max(x.shape[0] for x in new_input_embeds)
        batch_size = len(new_input_embeds)
        new_input_embeds_padded = torch.zeros((batch_size, max_len, self.args.n_embd), dtype=emb_dtype, device=device)
        new_labels_padded = torch.full((batch_size, max_len), IGNORE_INDEX, dtype=label_dtype, device=device)
        for i, (cur_new_embed, cur_new_labels) in enumerate(zip(new_input_embeds, new_labels)):
            cur_len = cur_new_embed.shape[0]
            new_input_embeds_padded[i, :cur_len] = cur_new_embed
            new_labels_padded[i, :cur_len] = cur_new_labels
        return new_input_embeds_padded, new_labels_padded
    
    def generate(self, input_ids, images, do_sample, temperature, top_p, max_new_tokens, stop_token_idx) -> list[int]:
        ''' one mode to generate, only generate one sample at a time
        # input_ids: [1, seq_len]
        # images: [1, 3, 224, 224]
        # do_sample: bool
        # temperature: float
        # top_p: float
        # max_new_tokens: int
        '''
        # prepare samples
        samples = {"input_ids": input_ids, "images": images, "labels": torch.full_like(input_ids, IGNORE_INDEX)}
        # prepare embedding, x: [1, seq_len, n_embd]
        image_features  = self.encode_images(samples["images"], do_proj=True)
        # prepare embedding
        x, _ = self.preparing_embedding(samples, image_features, truncate=False)
        # generate
        generated = []
        for i in range(max_new_tokens):
            logits = self.rwkv(x)[:, -1, :]
            if do_sample:
                raise NotImplementedError
            else: # greedy
                # [1, vocab_size] -> [1, 1]
                next_token = torch.argmax(logits, dim=-1, keepdim=True)
            generated.append(next_token.item())
            if generated[-1] == stop_token_idx:
                break
            x = torch.cat((x, self.rwkv.emb(next_token)), dim=-2)
            x = x[:, -self.args.ctx_len:, :] # truncate
        return generated
    

########################################################################################################
# The constraive alignment module for VisualRWKV
########################################################################################################
class ContrastiveAlignment(nn.Module):
    def __init__(self, proj, queue_size, text_max_len, vision_max_len, text_embed_dim, vision_embed_dim, reduction='mean'):
        super().__init__()
        self.proj = proj
        self.queue_size = queue_size
        self.text_max_len = text_max_len
        self.vision_max_len = vision_max_len
        self.text_embed_dim = text_embed_dim
        self.vision_embed_dim = vision_embed_dim
        self.reduction = reduction
        self.register_buffer("text_queue", torch.zeros(queue_size, text_max_len, text_embed_dim))
        self.register_buffer("vision_queue", torch.zeros(queue_size, vision_max_len, vision_embed_dim))
        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

    def pool_features(self, text_embeds, vision_features, text_masks=None):
        if text_masks is None:
            text_masks = torch.ones_like(text_embeds[..., 0], dtype=torch.bool)
        if self.reduction == 'mean':
            vision_pooled_features = vision_features.mean(dim=1)
            text_pooled_embeds = text_embeds.sum(dim=1) / text_masks.sum(dim=-1, keepdim=True)
        elif self.reduction == 'weighted':
            text2vision_similarities = torch.einsum('ntd,nvd->ntv', text_embeds, vision_features)
            # mask out padding tokens
            text_embeds_weights = text2vision_similarities.mean(dim=-1) + (~text_masks) * (-1e9)
            text_embeds_weights = torch.softmax(text2vision_similarities.mean(dim=-1), dim=-1) # [N, T]
            text_pooled_embeds = torch.einsum('ntd,nt->nd', text_embeds, text_embeds_weights)
            vision_features_weights = torch.softmax(text2vision_similarities.mean(dim=-2), dim=-1) # [N, V]
            vision_pooled_features = torch.einsum('nvd,nv->nd', vision_features, vision_features_weights)
        else:
            raise ValueError(f"Unknown reduction: {self.reduction}")
        return text_pooled_embeds, vision_pooled_features

    def compute_in_batch_constraive_loss(self, text_embeds, vision_features, text_masks):
        # first pool the vision features and text embeds
        text_pooled_embeds, vision_pooled_features = self.pool_features(text_embeds, vision_features, text_masks)
        # Calculate pairwise similarity
        t2v_matrix = text_pooled_embeds @ vision_pooled_features.T # [N, N]
        v2t_matrix = vision_pooled_features @ text_pooled_embeds.T # [N, N]
        # Calculate the loss
        labels = torch.arange(text_embeds.shape[0], device=text_embeds.device)
        t2v_loss = F.cross_entropy(t2v_matrix, labels, label_smoothing=0.1)
        v2t_loss = F.cross_entropy(v2t_matrix, labels, label_smoothing=0.1)
        return (t2v_loss + v2t_loss) / 2
    
    def compute_in_queue_constraive_loss(self, text_embeds, vision_features, text_masks, text_neg_embeds, vision_neg_features):
        # first pool the vision features and text embeds
        text_pooled_embeds, vision_pooled_features = self.pool_features(text_embeds, vision_features, text_masks)
        text_neg_pooled_embeds, vision_neg_pooled_features = self.pool_features(text_neg_embeds, vision_neg_features, text_masks=None)
        # Calculate pos logits
        pos_logits = torch.einsum('nd,nd->n', text_pooled_embeds, vision_pooled_features)
        # Calculate neg logits
        neg_text_logits = torch.einsum('nd,kd->nk', text_pooled_embeds, vision_neg_pooled_features)
        neg_vision_logits = torch.einsum('nd,kd->nk', vision_pooled_features, text_neg_pooled_embeds)
        # Calculate the loss
        # logits: [batch_size, 1+K+K]
        logits = torch.cat((pos_logits.unsqueeze(-1), neg_text_logits, neg_vision_logits), dim=-1)
        # labels: positive key indicators, which is 0
        labels = torch.zeros(text_embeds.shape[0], dtype=torch.long, device=text_embeds.device)
        # compute constraive loss
        loss = F.cross_entropy(logits, labels, label_smoothing=0.1)
        return loss

    def forward(self, text_embeds, vision_embeds, text_masks):
        # text_embeds: [batch_size, text_max_len, embed_dim]
        # vision_embeds: [batch_size, vision_max_len, embed_dim]
        batch_size = text_embeds.shape[0]
        # project to the same space
        vision_features = self.proj(vision_embeds)
        vision_neg_embeds = self.vision_queue.clone().detach()
        vision_neg_features = self.proj(vision_neg_embeds)
        # apply mask, make padding tokens zero
        text_embeds = text_embeds * text_masks.unsqueeze(-1)
        text_neg_embeds = self.text_queue.clone().detach()
        # compute loss, when batch_size == 1, only compute in_queue loss
        if batch_size != 1:
            in_batch_loss = self.compute_in_batch_constraive_loss(text_embeds, vision_features, text_masks)
            in_queue_loss = self.compute_in_queue_constraive_loss(text_embeds, vision_features, text_masks, text_neg_embeds, vision_neg_features)
            loss = in_batch_loss + in_queue_loss
        else:
            loss = self.compute_in_queue_constraive_loss(text_embeds, vision_features, text_masks, text_neg_embeds, vision_neg_features)
        # update queue
        self.text_queue[self.queue_ptr:self.queue_ptr+batch_size, :] = text_embeds
        self.vision_queue[self.queue_ptr:self.queue_ptr+batch_size, :] = vision_embeds
        self.queue_ptr = (self.queue_ptr + batch_size) % self.queue_size
        return loss