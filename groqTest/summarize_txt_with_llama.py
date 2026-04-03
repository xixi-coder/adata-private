#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


DEFAULT_MAX_TEXT_CHARS = 8_000


def load_llama_module(llama_path: Path):
    spec = importlib.util.spec_from_file_location("local_llama_module", str(llama_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from: {llama_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_prompt(transcript: str, max_chars: int) -> str:
    clipped = transcript[:max_chars]
    clipped_note = ""
    if len(transcript) > max_chars:
        clipped_note = (
            f"\n\nNote: transcript was clipped to first {max_chars} characters to control token usage."
        )
    return (
        "You are a concise Chinese summarization assistant.\n"
        "Please summarize the transcript content below.\n\n"
        "Output format:\n"
        "1) Core conclusions (3-5 bullets)\n"
        "2) Topic breakdown\n"
        "3) Actionable follow-ups (<=3 bullets)\n"
        "4) Key quotes or statements worth attention\n\n"
        "Requirements:\n"
        "- Answer in Chinese.\n"
        "- Do not invent facts.\n"
        "- If transcript is unclear/noisy, explicitly state uncertainty.\n\n"
        "Transcript:\n"
        f"{clipped}{clipped_note}"
    )


def summarize_txt(
    input_txt: Path,
    output_path: Path,
    llama_file: Path,
    model: str | None,
    api_key: str | None,
    max_chars: int,
) -> None:
    if not input_txt.exists():
        raise FileNotFoundError(f"Transcript file not found: {input_txt}")

    transcript = input_txt.read_text(encoding="utf-8", errors="ignore").strip()
    if not transcript:
        raise RuntimeError("Transcript file is empty.")

    llama_module = load_llama_module(llama_file)
    default_model = getattr(llama_module, "DEFAULT_MODEL", "llama-3.3-70b-versatile")
    final_model = model or default_model

    if hasattr(llama_module, "_build_client"):
        client = llama_module._build_client(api_key=api_key)
    else:
        raise RuntimeError("llama.py does not expose _build_client; cannot reuse its model client config.")

    prompt = build_prompt(transcript, max_chars=max_chars)
    resp = client.chat.completions.create(
        model=final_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    summary = (resp.choices[0].message.content or "").strip()
    if not summary:
        raise RuntimeError("Model returned empty summary.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(summary + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a transcript txt with model config from groqTest/llama.py.")
    parser.add_argument("--input-txt", required=True, help="Path to transcript txt file.")
    parser.add_argument("--output", required=True, help="Path to write summary txt.")
    parser.add_argument(
        "--llama-file",
        default=str(Path(__file__).resolve().parent / "llama.py"),
        help="Path to llama.py (default: groqTest/llama.py).",
    )
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument("--api-key", default=None, help="Optional API key override.")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_TEXT_CHARS,
        help=f"Max transcript characters sent to model (default: {DEFAULT_MAX_TEXT_CHARS}).",
    )
    args = parser.parse_args()

    summarize_txt(
        input_txt=Path(args.input_txt).resolve(),
        output_path=Path(args.output).resolve(),
        llama_file=Path(args.llama_file).resolve(),
        model=args.model,
        api_key=args.api_key,
        max_chars=args.max_chars,
    )
    print(f"Summary written to: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
