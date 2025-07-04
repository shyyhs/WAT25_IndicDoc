#!/usr/bin/env python
# inference_vllm.py
import argparse
import json
import sys
import os
from tqdm import tqdm
from pathlib import Path
import torch

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input_file", required=True)
    p.add_argument("--output_file", required=True)
    p.add_argument("--model", required=True,
                   help="HF repo path or local dir; e.g. meta-llama/Llama-3.1-8B")
    p.add_argument("--max_new_tokens", type=int, default=256)
    p.add_argument("--sampling", action="store_true")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--top_p", type=float, default=0.9)
    p.add_argument("--batch_size", type=int, default=4)
    return p.parse_args()

def load_prompts(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line: continue
        obj = json.loads(line) if line[0] in "{[" else {"prompt": line}
        if isinstance(obj, list): yield obj[0]
        else: yield obj["prompt"]

def build_generator(args):
    # Import vLLM here to control error handling
    from vllm import LLM, SamplingParams
    
    # Work around the UUID issue by patching vLLM's device_id_to_physical_device_id function
    import vllm.platforms.cuda as cuda_platform
    
    # Store the original function
    original_device_id_fn = cuda_platform.device_id_to_physical_device_id
    
    # Define a patched function that safely handles UUID device IDs
    def patched_device_id_fn(device_id):
        try:
            return original_device_id_fn(device_id)
        except ValueError:
            # If UUID-like device ID is encountered, return a safe index based on current context
            print(f"Warning: Encountered non-integer device ID: {device_id}, using index 0 instead")
            return 0
    
    # Apply the patch
    cuda_platform.device_id_to_physical_device_id = patched_device_id_fn
    
    # Initialize vLLM engine with safe settings for problematic environments
    print(f"Initializing vLLM for model: {args.model}")
    llm = LLM(
        model=args.model,
        tensor_parallel_size=1,  # Use single GPU tensor parallelism for stability
        dtype="bfloat16",
        trust_remote_code=True,
        gpu_memory_utilization=0.85,  # Be conservative with memory
    )
    
    # Configure sampling parameters
    sampling_params = SamplingParams(
        max_tokens=args.max_new_tokens,
        temperature=args.temperature if args.sampling else 0.0,
        top_p=args.top_p if args.sampling else 1.0,
        stop=["\n\n"],
    )
    
    # Define a generator function that uses vLLM for batched inference
    def generate(prompts):
        outputs = llm.generate(prompts, sampling_params)
        return [
            {"generated_text": prompt + output.outputs[0].text}
            for prompt, output in zip(prompts, outputs)
        ]
    
    return generate

def main():
    args = parse_args()
    prompts = list(load_prompts(args.input_file))
    print(f"Loaded {len(prompts)} prompts.")
    
    # Create output directory if it doesn't exist
    os.makedirs(os.path.dirname(args.output_file), exist_ok=True)
    
    # Initialize vLLM generator
    gen_fn = build_generator(args)
    print(f"Starting batched inference with vLLM (batch size: {args.batch_size})...")
    
    with open(args.output_file, "w", encoding="utf-8") as fout:
        for i in tqdm(range(0, len(prompts), args.batch_size), desc="Generating"):
            batch_prompts = prompts[i:i + args.batch_size]
            batch_outputs = gen_fn(batch_prompts)
            
            for output, prompt in zip(batch_outputs, batch_prompts):
                generation = output["generated_text"].replace(prompt, "").strip()
                fout.write(json.dumps([generation], ensure_ascii=False) + "\n")
            fout.flush()

            # Report memory usage for monitoring
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                used_memory = torch.cuda.max_memory_allocated() / (1024 ** 2)
                print(f"Max GPU memory used: {used_memory:.2f} MB")
    
    print("vLLM inference completed successfully.")

if __name__ == "__main__":
    main()