export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5

# 切换到脚本所在目录的上两级目录
cd "$(dirname "$(dirname "$0")")/.."

# 打印当前工作目录
echo "Current working directory: $(pwd)"


python train.py --model_path out/rwkv7b-v060_pretrain/rwkv-17.pth \
    --wandb "" --proj_dir out/rwkv7b-v060_mix665k \
    --data_file /houhaowenT/huggingface_datasets/LLaVA-Instruct-150K/shuffled_llava_v1_5_mix665k_reformatted.json \
    --data_type "json" --vocab_size 65536 \
    --ctx_len 2048 --epoch_steps 1000 --epoch_count 227 --epoch_begin 0 --epoch_save 57 \
    --micro_bsz 1 --accumulate_grad_batches 22 --n_layer 32 --n_embd 4096 --pre_ffn 0 \
    --lr_init 2e-5 --lr_final 2e-5 --warmup_steps 0 --beta1 0.9 --beta2 0.99 --adam_eps 1e-8 \
    --accelerator gpu --devices 6 --precision bf16 --strategy deepspeed_stage_2 --grad_cp 0 \
    --image_folder /houhaowenT/huggingface_datasets/LLaVA-Instruct-150K/images/ \
    --vision_tower_name /houhaowenT/huggingface_models/openai/clip-vit-large-patch14-336 \
    --freeze_rwkv 0 --freeze_proj 1 --detail low --grid_size -1 --image_position middle \
    --enable_progress_bar True
