import argparse
from pathlib import Path
from functools import partial

from tqdm.auto import tqdm

import torch
import torch.nn as nn

from binary_diffusion_tabular import (
    BinaryDiffusion1D,
    SimpleTableGenerator,
    select_equally_distributed_numbers,
    TASK,
    get_random_labels
)


def get_sampling_args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ckpt", type=str, required=True, help="Path to checkpoint file"
    )
    parser.add_argument(
        "--path_test_data",
        type=str,
        required=True,
        help="Path to test data in .csv format",
    )
    parser.add_argument(
        "--n_timesteps", "-t", type=int, required=True, help="Number of sampling steps"
    )
    parser.add_argument(
        "--out",
        "-o",
        type=str,
        required=True,
        help="Path to output folder, where to save samples",
    )
    parser.add_argument(
        "--n_samples",
        "-n",
        type=int,
        required=True,
        help="Number of samples to generate",
    )
    parser.add_argument(
        "--batch_size", "-b", type=int, required=True, help="Batch size for sampling"
    )
    parser.add_argument(
        "--threshold", "-t", type=float, default=0.5, help="Threshold for binarization"
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="target",
        choices=["target", "mask"],
        help="Sampling strategy to use",
    )
    parser.add_argument("--seed", "-s", type=int, help="Random seed")
    parser.add_argument(
        "--guidance_scale", "-g", type=float, default=0.0, help="Guidance scale"
    )
    parser.add_argument("--device", "-d", type=str, default="cuda", help="Device")
    parser.add_argument("--use_ema", "-e", action="store_true", help="Use EMA")

    return parser


def cfg_model_fn(
    x_t: torch.Tensor,
    ts: torch.Tensor,
    y: torch.Tensor,
    model: nn.Module,
    guidance_scale: float,
    task: TASK,
    *args, **kwargs
) -> torch.Tensor:
    """Classifier free guidance sampling function

    Args:
        x_t: noisy sample
        ts: timesteps
        y: conditioning
        model: denoising model
        guidance_scale: guidance scale in classifier free guidance
        task: dataset task

    Returns:
        torch.Tensor: denoiser output
    """

    combine = torch.cat([x_t, x_t], dim=0)
    combine_ts = torch.cat([ts, ts], dim=0)

    if task == "classification":
        y_other = torch.zeros_like(y)
    elif task == "regression":
        # for regression, zero-token is -1, since values are minmax normalized to [0, 1] range
        y_other = torch.ones_like(y) * -1

    combine_y = torch.cat([y, y_other], dim=0)
    model_out = model(combine, combine_ts, y=combine_y)
    cond_eps, uncod_eps = torch.split(model_out, [y.shape[0], y.shape[0]], dim=0)
    eps = uncod_eps + guidance_scale * (cond_eps - uncod_eps)
    return eps


if __name__ == "__main__":
    parser = get_sampling_args_parser()
    cli_args = parser.parse_args()

    path_out = Path(cli_args.out)
    path_out.mkdir(parents=True, exist_ok=True)

    ckpt = torch.load(cli_args.ckpt)
    device = cli_args.device
    batch_size = int(cli_args.batch_size)
    guidance_scale = cli_args.guidance_scale
    threshold = cli_args.threshold
    strategy = cli_args.strategy

    denoising_model = SimpleTableGenerator.from_config(ckpt["config_model"]).to(device)
    denoising_model.eval()

    diffusion = BinaryDiffusion1D.from_config(
        denoise_model=denoising_model,
        config=ckpt["config_diffusion"],
    ).to(device)
    diffusion.eval()

    if cli_args.use_ema:
        diffusion.load_ema(ckpt["diffusion_ema"])
    else:
        diffusion.load_state_dict(ckpt["diffusion"])

    n_total_timesteps = diffusion.n_timesteps
    timesteps_sampling = select_equally_distributed_numbers(
        n_total_timesteps,
        cli_args.n_timesteps,
    )
    task = denoising_model.task
    conditional = denoising_model.conditional
    n_classes = denoising_model.n_classes
    classifier_free_guidance = denoising_model.classifier_free_guidance

    n_generated = 0
    n_samples = cli_args.n_samples
    pbar = tqdm(total=n_samples)

    while n_generated < n_samples:
        labels = get_random_labels(
            conditional=conditional,
            task=task,
            n_classes=n_classes,
            classifier_free_guidance=classifier_free_guidance,
            n_labels=batch_size,
            device=device,
        )

        x = diffusion.sample(
            model_fn=partial(cfg_model_fn, guidance_scale=guidance_scale, task=task) if classifier_free_guidance and guidance_scale > 0 else None,
            n=batch_size,
            y=labels,
            timesteps=timesteps_sampling,
            threshold=threshold,
            strategy=strategy
        )

