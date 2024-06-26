########################################################################################################
# The RWKV Language Model - https://github.com/BlinkDL/RWKV-LM
########################################################################################################

import json, os, re, copy
import numpy as np
from PIL import Image, ImageFile
import torch
from torch.utils.data import Dataset
from pytorch_lightning.utilities import rank_zero_info
from typing import Dict, List, Sequence, Any
from .utils import gpt4v_crop, largest_3n_plus_2_prime
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Model Constants
IGNORE_INDEX = -100
IMAGE_TOKEN_INDEX = -200
PAD_TOKEN_INDEX = 0
DEFAULT_IMAGE_TOKEN = "<image>"
STOP_TOKEN_INDEX = 261
DEFAULT_STOP_TOKEN = "\n\n"


def process_image_tokens_in_conversations(
    conversations: Sequence[Dict],
) -> Sequence[Dict]:
    """
    Process image tokens within conversations.
    image first, then text
    replace \n\n with \n
    """
    for sentence in conversations:
        if DEFAULT_IMAGE_TOKEN in sentence['value']:
            sentence['value'] = sentence['value'].replace(DEFAULT_IMAGE_TOKEN, '').strip()
            sentence['value'] = re.sub(r"\n(\s*\n)+", '\n', sentence['value'])
            sentence['value'] = DEFAULT_IMAGE_TOKEN + '\n' + sentence['value']
            sentence['value'] = sentence['value'].strip()
        else:
            sentence['value'] = re.sub(r"\n(\s*\n)+", '\n', sentence['value'].strip())

    return conversations

def process_tokens_in_conversations(
    conversations: Sequence[Dict],
) -> Sequence[Dict]:
    """
    Process tokens within conversations.
    replace \n\n with \n
    """
    for sentence in conversations:
        sentence['value'] = sentence['value'].strip()
        sentence['value'] = re.sub(r"\n(\s*\n)+", '\n', sentence['value'])

    return conversations


def _add_speaker_and_signal(conversations):
    """Add speaker and start/end signal on each round."""
    for sentence in conversations:
        from_str = sentence["from"]
        if from_str.lower() == "human":
            from_str = "User"
        elif from_str.lower() == "gpt":
            from_str = "Assistant"
        else:
            raise ValueError(f"Unknown speaker: {from_str}, must be human or gpt.")
        
        if sentence["value"]: # for training, add end signal
            sentence["value"] = (from_str + ": " + sentence["value"] + DEFAULT_STOP_TOKEN)
        else: # for inference, not add end signal and no whitespace after colon
            sentence["value"] = from_str + ":"
    return conversations

def tokenize_with_image_token(prompt, tokenizer, image_token_index=IMAGE_TOKEN_INDEX):
    prompt_chunks = [tokenizer.encode(chunk) for chunk in prompt.split(DEFAULT_IMAGE_TOKEN)]

    input_ids = []
    for chunk in prompt_chunks:
        input_ids.extend(chunk)
        input_ids.append(image_token_index)

    return input_ids[:-1] # remove last image token



def mask_targets_from_human(targets, tokenized_lens, speakers):
    cur_idx = 0
    for tokenized_len, speaker in zip(tokenized_lens, speakers):
        if speaker == "human":
            targets[cur_idx:cur_idx + tokenized_len] = IGNORE_INDEX
        cur_idx += tokenized_len

def pad_to_max_len(input_ids, targets, max_len, pad_token_id):
    # keep the last max_len targets, because a lot of first tokens are masked
    input_ids = input_ids[-max_len:]
    targets = targets[-max_len:]
    padding_len = max_len - len(input_ids)
    if padding_len <= 0:
        return input_ids, targets
    # input_ids and targets are tensors
    input_ids = torch.cat([input_ids, torch.tensor([pad_token_id] * padding_len, dtype=torch.long)])
    targets = torch.cat([targets, torch.tensor([IGNORE_INDEX] * padding_len, dtype=torch.long)])
    return input_ids, targets


def preprocess(conversations, tokenizer, has_image, ctx_len, pad_token_id=0, do_pad_to_max_length=True):
    """
    Given a list of sources, each is a conversation list. This transform:
    1. Add \n\n after each round;
    2. Concatenate conversations together;
    3. Tokenize the concatenated conversation;
    4. Make a deepcopy as the target. Mask human words with IGNORE_INDEX.
    5. Pad to max length.
    """
    # add end signal and concatenate together
    conversations = _add_speaker_and_signal(conversations)
    conversation = "".join([sentence["value"] for sentence in conversations])
    if has_image:
        input_ids = tokenize_with_image_token(conversation, tokenizer)
    else:
        input_ids = tokenizer.encode(conversation)
    input_ids = torch.tensor(input_ids, dtype=torch.long)
    targets = copy.deepcopy(input_ids)
    if has_image:
        tokenized_lens = [len(tokenize_with_image_token(s["value"], tokenizer)) for s in conversations]
    else:
        tokenized_lens = [len(tokenizer.encode(s["value"])) for s in conversations]
    speakers = [sentence["from"] for sentence in conversations]
    mask_targets_from_human(targets, tokenized_lens, speakers)
    if do_pad_to_max_length:
        input_ids, targets = pad_to_max_len(input_ids, targets, ctx_len, pad_token_id)
    return dict(input_ids=input_ids, labels=targets, input_text=conversation)



class MyDataset(Dataset):
    def __init__(self, args):
        self.args = args
        self.vocab_size = args.vocab_size
        self.tokenizer = args.tokenizer
        self.list_data_dict = json.load(open(args.data_file, "r"))
        self.data_size = len(self.list_data_dict)
        self.magic_prime = largest_3n_plus_2_prime(self.data_size)
        self.samples_per_epoch = self.args.epoch_steps * self.args.real_bsz

    def __len__(self):
        return self.args.epoch_steps * self.args.micro_bsz

    def __getitem__(self, idx):
        args = self.args
        rank = self.global_rank
        epoch = self.real_epoch
        world_size = self.world_size
        step = epoch * self.samples_per_epoch + (idx * world_size) + rank
        # use a magic prime to sample the dataset deterministically yet randomly enough
        sample_idx = (step * step * step) % self.magic_prime
        # print(f"step {step} sample idx {sample_idx} epoch {epoch} idx {idx} rank {rank}/{world_size} magic prime {self.magic_prime}")
        sample = self.list_data_dict[sample_idx]
        if 'image' in sample:
            image_file = sample['image']
            image_folder = args.image_folder
            processor = args.image_processor
            image = Image.open(os.path.join(image_folder, image_file)).convert('RGB')
            if args.detail == 'high':
                image = [image] + gpt4v_crop(image)
                image = processor(images=image, return_tensors='pt')['pixel_values']
            else:
                image = processor.preprocess(image, return_tensors='pt')['pixel_values']
            conversations = process_image_tokens_in_conversations(copy.deepcopy(sample["conversations"]))
        else:
            conversations = process_tokens_in_conversations(copy.deepcopy(sample["conversations"]))

        data_dict = preprocess(
            conversations,
            self.tokenizer,
            has_image=('image' in sample),
            ctx_len=args.ctx_len,
            pad_token_id=PAD_TOKEN_INDEX)
        
        # image exist in the data
        if 'image' in sample:
            data_dict['images'] = image
        else:
            # image does not exist in the data, fill with zeros
            if args.detail == 'high':
                crop_size = args.image_processor.crop_size
                data_dict['images'] = torch.zeros(7, 3, crop_size['height'], crop_size['width'])
            else:
                crop_size = args.image_processor.crop_size
                data_dict['images'] = torch.zeros(1, 3, crop_size['height'], crop_size['width'])
        return data_dict
    

if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument("--data_file", type=str, default="data.json")
    parser.add_argument("--vocab_size", type=int, default=10000)
    parser.add_argument("--image_folder", type=str, default="images")
    parser.add_argument("--image_processor", type=str, default="images")
    parser.add_argument("--tokenizer", type=str, default="images")
    parser.add_argument("--epoch_steps", type=int, default=100)
    parser.add_argument("--micro_bsz", type=int, default=1)
    parser.add_argument("--ctx_len", type=int, default=512)
    args = parser.parse_args()
    from rwkv.rwkv_tokenizer import TRIE_TOKENIZER
    from transformers import CLIPImageProcessor
    args.tokenizer = TRIE_TOKENIZER("rwkv_vocab_v20230424.txt")
    args.image_processor = CLIPImageProcessor.from_pretrained("openai/clip-vit-base-patch32")
    dataset = MyDataset(args)
    print(dataset[0])
