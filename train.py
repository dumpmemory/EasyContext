import argparse
import copy
import torch
import os
from datasets import load_dataset, load_from_disk, DatasetDict
from datetime import timedelta
from torch.utils.data import DataLoader
from accelerate import Accelerator
from accelerate.utils import InitProcessGroupKwargs, set_seed
from tqdm import tqdm
from transformers import set_seed, default_data_collator, get_linear_schedule_with_warmup, get_constant_schedule_with_warmup
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP, StateDictType, FullStateDictConfig
from modeling.modeling_llama import LlamaForCausalLM, LlamaDecoderLayer
from flash_attn.losses.cross_entropy import CrossEntropyLoss
import math
import functools
from accelerate.utils import InitProcessGroupKwargs, set_seed, DummyOptim, DummyScheduler
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy

def main(args):
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
    if args.wandb:
        import wandb
        wandb.login()
    set_seed(args.seed)

    timeout = InitProcessGroupKwargs(timeout=timedelta(seconds=1_000_000))

    auto_wrap_policy = functools.partial(
        transformer_auto_wrap_policy,
        transformer_layer_cls={
            LlamaDecoderLayer,
        },
    )
    # fsdp_plugin = FullyShardedDataParallelPlugin(
    #     sharding_strategy=ShardingStrategy.FULL_SHARD,
    #     auto_wrap_policy=auto_wrap_policy,
    #     activation_checkpointing=False,
    # )
    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulate_every,
        mixed_precision="bf16",
        log_with="wandb" if args.wandb else None,
        kwargs_handlers=[timeout],
        # fsdp_plugin=fsdp_plugin,
    )
    accelerator.init_trackers(
        project_name=args.wandb)
    accelerator.print(f"Total GPUS: {accelerator.num_processes}")



    try:
        train_dataset = load_dataset(args.dataset, num_proc=64 )
    except:
        train_dataset = load_from_disk(args.dataset)
    if isinstance(train_dataset, DatasetDict):
        train_dataset = train_dataset["train"]

    model = LlamaForCausalLM.from_pretrained(
        args.model,
        device_map=accelerator.device,
        torch_dtype=torch.bfloat16,
        rope_theta = args.rope_theta,
        _attn_implementation="flash_attention_2"
    )
    if "input_ids" not in train_dataset.column_names:
        raise RuntimeError("Dataset must include an `input_ids` feature")
    # remove everything that is not input_ids
    to_remove = [col for col in train_dataset.column_names if col != "input_ids"]
    train_dataset = train_dataset.remove_columns(to_remove)
    train_dataset = train_dataset.shuffle(seed=args.seed)
    print("Dataset Size:", len(train_dataset))
    train_loader = DataLoader(
        train_dataset,
        collate_fn=default_data_collator,
        shuffle=True,
        batch_size=args.batch_size,
    )

    optim = DummyOptim(model.parameters(), lr=args.learning_rate)
    scheduler = DummyScheduler(
        optim, num_training_steps=args.max_train_steps, total_num_steps=args.max_train_steps, num_warmup_steps=args.warmup_steps)
    model, optim, scheduler = accelerator.prepare(
        model, optim, scheduler
    )
    # when using ring attention, we need to make sure each process load the same data. So do not prepare it with accelerator
    if not args.ring_attention:
        train_loader = accelerator.prepare(train_loader)


    model.gradient_checkpointing_enable()

    accelerator.register_for_checkpointing(scheduler)


    accelerator.print(f"Max train steps: {args.max_train_steps}")
    progress_bar = tqdm(
        range(args.max_train_steps), disable=not accelerator.is_local_main_process
    )
    completed_steps = 0





    model.train()
    loss_func = CrossEntropyLoss(inplace_backward=True)
    for step, batch in enumerate(train_loader):
        input_ids = batch["input_ids"][..., :args.seq_length+1][..., :-1]
        target_ids = batch["input_ids"][..., :args.seq_length+1][..., 1:]
        position_ids = torch.arange(args.seq_length).unsqueeze(0).expand(input_ids.shape[0], -1)
        # shard the input_ids according to the world size and rank according to zig zag attention

        def extract_local(value, rank, world_size, device, dim=1):
            value_chunks = value.chunk(2 * world_size, dim=dim)
            local_value = torch.cat(
                [value_chunks[rank], value_chunks[2 * world_size - rank - 1]], dim=dim
            )
            return local_value.to(device)
        local_input_ids = extract_local(input_ids, accelerator.process_index, accelerator.num_processes, accelerator.device) if args.ring_attention else input_ids.to( accelerator.device)
        local_target_ids = extract_local(target_ids, accelerator.process_index, accelerator.num_processes, accelerator.device) if args.ring_attention else target_ids.to( accelerator.device)
        local_position_ids = extract_local(position_ids, accelerator.process_index, accelerator.num_processes, accelerator.device) if args.ring_attention else position_ids.to( accelerator.device)
        loss_log = None
        with accelerator.accumulate(model):
            logits = model(local_input_ids, position_ids = local_position_ids, ring_attention=args.ring_attention).logits
            loss = loss_func(logits.reshape(-1, logits.shape[-1]), local_target_ids.reshape(-1))
            accelerator.backward(loss)

            if accelerator.sync_gradients:
                loss_log = {"loss": loss.item(), "ppl": math.exp(loss.item())}
                accelerator.log(loss_log, step=completed_steps)


            optim.step()
            scheduler.step()
            optim.zero_grad()

        if accelerator.sync_gradients:
            progress_bar.update(1)
            if loss_log is not None:
                progress_bar.set_postfix(loss_log)
            completed_steps += 1


        if completed_steps >= args.max_train_steps:
            break

    accelerator.print(f"Training Finished")
    accelerator.end_training()

    if args.output_dir is not None:
        accelerator.print(f"Saving model to {args.output_dir}")

        accelerator.wait_for_everyone()


        state_dict = accelerator.get_state_dict(model)


        accelerator.unwrap_model(model).save_pretrained(
            f"{args.output_dir}",
            is_main_process=accelerator.is_main_process,
            save_function=accelerator.save,
            state_dict=state_dict,
        )

        accelerator.print(f"Saving Finished")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--batch-size", type=int, default=1)
    args.add_argument("--gradient-accumulate-every", type=int, default=8)
    args.add_argument("--output-dir", type=str, required=True)
    args.add_argument("--lora", action="store_true")
    args.add_argument("--wandb", type=str)
    args.add_argument("--seed", type=int, default=42)
    args.add_argument("--max-train-steps", type=int, default=400)
    args.add_argument("--warmup-steps", type=int, default=20)
    args.add_argument("--learning-rate", type=float, default=2e-5)
    args.add_argument("--rope-theta", type=float, default=100000)
    args.add_argument("--model", type=str,
                      default="meta-llama/Llama-2-7b-hf")
    args.add_argument("--dataset", type=str,
                      default="emozilla/pg_books-tokenized-bos-eos-chunked-65536")
    args.add_argument("--num-proc", type=int, default=32)
    args.add_argument("--lr-schedule", type=str,
                      choices=["linear", "constant"], default="linear")
    args.add_argument("--log-loss", type=str)
    args.add_argument("--seq-length", type=int, default=16384)
    args.add_argument("--debug", action="store_true")
    args.add_argument("--ring_attention", action="store_true")
    main(args.parse_args())