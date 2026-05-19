"""Evaluate EPFLemma on RLM25 full autoformalization tasks.

This harness is intentionally external to both repositories.
It uses only the benchmark problem artifacts from RLM25 traced outputs:

- benchmark config for repository list
- blueprint_to_lean.jsonl for natural-language statement/proof problems
- lean_files.jsonl for original Lean file contents

It does not rely on RLMEval evaluation utilities, except for semantic checking.
Instead it creates a scratch
Lean file per benchmark problem containing the original pre-theorem context and
asks EPFLemma to generate the entire theorem declaration and proof.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonlines
import lean_interact
import yaml
from lean_interact import AutoLeanServer, LeanREPLConfig, LocalProject
from lean_interact.interface import Command, LeanError
from lean_interact.utils import extract_last_theorem, clean_theorem_string


HARNESS_ROOT = Path(__file__).resolve().parent
EPFLEMMA_ROOT = HARNESS_ROOT.parent
RLMEVAL_ROOT = EPFLEMMA_ROOT / "RLMEval"
DEFAULT_TRACED_REPOS_ROOT = RLMEVAL_ROOT / "traced_repos"
DEFAULT_OUTPUT_ROOT = HARNESS_ROOT / "epflemma_rlmeval_full_autoformalize_results"

if not EPFLEMMA_ROOT.is_dir():
	raise SystemExit(f"EPFLemma directory not found at {EPFLEMMA_ROOT}")

if str(EPFLEMMA_ROOT) not in sys.path:
	sys.path.insert(0, str(EPFLEMMA_ROOT))

rlm_eval_src = RLMEVAL_ROOT / "src"
if rlm_eval_src.is_dir() and str(rlm_eval_src) not in sys.path:
	sys.path.insert(0, str(rlm_eval_src))

mini_swe_src = EPFLEMMA_ROOT / "mini-swe-agent" / "src"
if mini_swe_src.is_dir() and str(mini_swe_src) not in sys.path:
	sys.path.insert(0, str(mini_swe_src))

from rlm_eval.data_processing.lean_utils import trim_comments_end
from rlm_eval.metrics.beq_plus import check_theorem_equivalence


def _log(message: str) -> None:
	"""Print progress lines immediately for long benchmark runs."""
	print(message, flush=True)


@dataclass
class ProblemResult:
	project_name: str
	node_label: str
	theorem_name: str
	source_file: str
	scratch_file: str
	model: str
	max_turns: int
	turns_used: int
	input_tokens: int
	output_tokens: int
	prefix_preserved: bool
	no_placeholders_left: bool
	lean_verified: bool
	beql: bool
	beq_plus: bool
	error: str | None = None
	duration_seconds: float = 0.0


def _sanitize_filename(text: str) -> str:
	"""Return a filesystem-safe short name for per-problem output files."""
	cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
	return cleaned[:120] or "problem"


def _clean_blueprint_text(text: str) -> str:
	"""Normalize blueprint text by splitting escaped TeX newlines."""
	raw = str(text or "")
	return "\n".join(line.strip() for line in raw.split(r"\\") if line.strip())



def _extract_prefix(original_file_content: str, lean_declaration: dict[str, Any]) -> str:
	"""Extract the exact pre-theorem Lean context from the source file."""
	start_idx = int(lean_declaration["start_idx"])
	return original_file_content[:start_idx]


def _build_placeholder_block(theorem_name: str, statement_text: str, proof_text: str) -> str:
	"""Build a single placeholder theorem block for full replacement by the agent."""
	return (
		"\n\n/- AUTOFORMALIZATION TASK\n"
		f"Theorem name: {theorem_name}\n\n"
		"Natural language statement:\n"
		f"{statement_text}\n\n"
		"Natural language proof:\n"
		f"{proof_text}\n\n"
		"Replace the placeholder theorem below with one complete Lean theorem declaration and proof.\n"
		"Preserve the theorem name exactly. Remove `sorry`, `admit`, `axiom`, and placeholders.\n"
		"-/\n\n"
		f"theorem {theorem_name} : False := by\n"
		"  sorry\n"
	)


def _build_problem_prompt(
	theorem_name: str,
	scratch_file_relative: str,
	source_file_relative: str,
	statement_text: str,
	proof_text: str,
) -> str:
	"""Create the user instruction that asks EPFLemma for full statement+proof generation."""
	return (
		f"Formalize the following theorem and proof into Lean 4 in `{scratch_file_relative}`.\n"
		f"The original source file for context is `{source_file_relative}`.\n"
		f"Use the exact theorem name `{theorem_name}`.\n"
		"Work only in the scratch file. The scratch file already contains the full original context before the target theorem.\n\n"
		"Natural language statement:\n"
		f"{statement_text}\n\n"
		"Natural language proof:\n"
		f"{proof_text}\n\n"
		"Replace the placeholder theorem at the end of the scratch file with one complete theorem declaration and proof. "
		"Do not modify the earlier context in the scratch file. Verify the scratch file before stopping."
	)


def _run_agent(
	prompt: str,
	project_root: str,
	model_cfg: dict[str, Any],
	dry_run: bool = False,
	thinking_log_path: str | None = None,
) -> tuple[int, int, int, str | None]:
	"""Run one EPFLemma attempt and return token usage, turns, and optional error."""
	if dry_run:
		# Dry-run still uses the real Lean environment; it only skips the agent call.
		# Sleep so it behaves like an idle polled agent instead of returning instantly.
		time.sleep(30)
		return (0, 0, 0, None)

	from run_agent import AIAgent

	token_budget = model_cfg.get("token_budget")
	token_budget_int = int(token_budget) if token_budget is not None else None
	budget_hit = False

	# NOTE: agent is referenced here via late-binding closure (assigned below).
	# The agent calls step_callback(api_call_count, prev_tools) — two positional args.
	def _step_callback(*args, **kwargs) -> None:
		"""Interrupt the run once cumulative token budget is reached."""
		nonlocal budget_hit
		if token_budget_int is None or budget_hit:
			return
		total_tokens = int(getattr(agent, "session_prompt_tokens", 0)) + int(
			getattr(agent, "session_completion_tokens", 0)
		)
		if total_tokens >= token_budget_int:
			budget_hit = True
			agent.request_interrupt(f"Token budget reached: {token_budget_int}")

	agent = AIAgent(
		# Keep the run deterministic/configurable from model YAML.
		model=str(model_cfg.get("model", "claude-sonnet-4-20250514")),
		base_url=(str(model_cfg.get("base_url", "") or "") or None),
		api_key=(str(model_cfg.get("api_key", "") or "") or None),
		provider=(str(model_cfg.get("provider", "") or "") or None),
		max_iterations=int(model_cfg.get("max_turns", 50)),
		max_tokens=(int(model_cfg["max_tokens"]) if model_cfg.get("max_tokens") is not None else None),
		temperature=float(model_cfg.get("temperature", 0.3)),
		seed=int(model_cfg.get("seed", 42)),
		quiet_mode=not bool(model_cfg.get("verbose", False)),
		verbose_logging=False,
		skip_context_files=True,
		skip_memory=True,
		step_callback=_step_callback if token_budget_int is not None else None,
	)

	# Keep terminal/file tools scoped to the traced project root.
	os.environ["TERMINAL_CWD"] = project_root
	try:
		# One prompt = one autonomous EPFLemma trajectory for this theorem.
		agent.run_conversation(user_message=prompt)
		error = "token_budget_reached" if budget_hit else None
	except Exception as exc:
		# Surface runtime failures while still returning measured token usage.
		error = f"{type(exc).__name__}: {exc}"

	# Write per-theorem thinking trace if a log path was requested.
	# Reasoning is extracted from msg["reasoning"] which _extract_reasoning() populates
	# from assistant_message.reasoning_content (Moonshot/Kimi), .reasoning (DeepSeek), etc.
	if thinking_log_path is not None:
		try:
			session_msgs = getattr(agent, "_session_messages", []) or []
			with open(thinking_log_path, "w", encoding="utf-8") as _tf:
				turn = 0
				for msg in session_msgs:
					if msg.get("role") == "assistant":
						turn += 1
						reasoning = msg.get("reasoning") or ""
						if reasoning.strip():
							_tf.write(f"=== Turn {turn} thinking ===\n{reasoning.strip()}\n\n")
		except Exception:
			pass  # Logging failure must never surface as a harness error.

	return (
		int(getattr(agent, "session_prompt_tokens", 0)),
		int(getattr(agent, "session_completion_tokens", 0)),
		int(getattr(agent, "_current_run_api_calls", 0)),
		error,
	)


def _verify_generated_tail(scratch_path: str, project_root: str, generated_tail: str) -> tuple[bool, bool]:
	"""Check generated theorem compiles sorry-free by running lake env lean on the scratch file.

	Follows the same pipeline as RLMEval's eval scripts: the full Lean file (prefix + generated
	declaration/proof) is already on disk at scratch_path; we invoke 'lake env lean' on it and
	parse stdout+stderr for compilation errors and sorry warnings.

	Returns (lean_verified, no_placeholders_left).
	"""
	if not generated_tail.strip():
		return False, False

	try:
		proc = subprocess.run(
			["lake", "env", "lean", scratch_path],
			cwd=project_root,
			capture_output=True,
			text=True,
			timeout=300,
		)
		out = proc.stdout + proc.stderr
		has_sorry = (
			"declaration uses 'sorry'" in out
			or "declaration uses `sorry`" in out
		)
		compiled_ok = proc.returncode == 0
		# lean_verified: compiled without errors and no sorry
		verified = compiled_ok and not has_sorry
		# no_placeholders_left: same condition — sorry is the only placeholder mechanism
		return verified, verified
	except subprocess.TimeoutExpired:
		return False, False
	except Exception:
		return False, False


def _compute_beq_checks(prefix: str, generated_tail: str, lean_decl: dict[str, Any], repl_config: LeanREPLConfig) -> tuple[bool, bool]:
	"""Compute RLMEval BEqL/BEq+ equivalence for generated vs ground-truth theorem.

	Directly mirrors RLMEval's eval_statement_autoformalization.py approach:
	- theorem1: just the generated theorem declaration (last theorem in generated_tail)
	- theorem2: clean_theorem_string(declsig_no_comments, add_sorry=True)
	- context: full original prefix with trim_comments_end + 'open ... in' handling
	"""
	try:
		thm_idx = extract_last_theorem(generated_tail)
		theorem1 = generated_tail[thm_idx:]
	except (ValueError, Exception):
		return False, False

	theorem_info = lean_decl.get("theorem_info") or {}
	declsig = theorem_info.get("declsig_no_comments") or lean_decl.get("decl", "")
	theorem_name = str(lean_decl.get("name") or "")
	theorem2 = clean_theorem_string(declsig, new_theorem_name=theorem_name, add_sorry=True)
	if not theorem1.strip() or not theorem2:
		return False, False

	# Canonical RLMEval context loading (mirrors eval_statement_autoformalization.py):
	# trim trailing comments, then handle 'open ... in' suffix on the last line.
	trimmed = trim_comments_end(prefix).rstrip()
	if "\n" in trimmed:
		lean_context, last_line = trimmed.rsplit("\n", 1)
	else:
		lean_context = ""
		last_line = trimmed
	if last_line.endswith(" in"):
		lean_context += "\n" + last_line[:-3]
	else:
		lean_context = prefix
	if not lean_context.strip():
		lean_context = "-- context stub"

	lean_server = AutoLeanServer(repl_config)
	try:
		ctx_out = lean_server.run(Command(cmd=lean_context), add_to_session_cache=True, timeout=360)
		if isinstance(ctx_out, LeanError) or not ctx_out.lean_code_is_valid():
			return False, False
		beq = check_theorem_equivalence(
			theorem1=theorem1,
			theorem2=theorem2,
			lean_server=lean_server,
			context_env=ctx_out.env,
			timeout_per_proof=120,
		)
		return beq.beql(), beq.beq_plus()
	except Exception:
		return False, False


def evaluate_problem(
	node: dict[str, Any],
	project_name: str,
	project_root: str,
	original_file_content: str,
	output_dir: str,
	model_cfg: dict[str, Any],
	repl_config: LeanREPLConfig,
	dry_run: bool = False,
) -> ProblemResult:
	"""Evaluate one benchmark node end-to-end and persist per-problem artifacts."""
	lean_decl = node["lean_declarations"][0]
	theorem_name = str(lean_decl.get("name") or node["label"])
	source_file = str(lean_decl["file"])
	prefix = _extract_prefix(original_file_content, lean_decl)
	# Problem text comes from blueprint extraction (statement + proof prose).
	statement_text = _clean_blueprint_text(node.get("processed_text", ""))
	proof_text = _clean_blueprint_text(node.get("proof", {}).get("text", ""))

	scratch_dir = os.path.join(project_root, ".epflemma_rlmeval_full_autoformalize")
	os.makedirs(scratch_dir, exist_ok=True)
	scratch_filename = _sanitize_filename(f"{Path(source_file).stem}__{node['label']}.lean")
	scratch_path = os.path.join(scratch_dir, scratch_filename)
	# Store scratch path relative to project root for stable reporting across machines.
	scratch_relative = os.path.relpath(scratch_path, project_root)

	# Scratch content is the immutable prefix plus a replace-me theorem placeholder.
	placeholder = _build_placeholder_block(theorem_name, statement_text, proof_text)
	initial_scratch = prefix + placeholder

	start_time = time.time()
	# Each benchmark node gets its own artifact directory (inputs, generated Lean, metrics).
	os.makedirs(output_dir, exist_ok=True)

	with open(os.path.join(output_dir, "problem.json"), "w", encoding="utf-8") as f:
		# Persist full problem payload for offline debugging/replay.
		# Includes ground-truth declaration used later by BEq checks.
		json.dump(
			{
				"project_name": project_name,
				"node_label": node["label"],
				"theorem_name": theorem_name,
				"source_file": source_file,
				"statement_text": statement_text,
				"proof_text": proof_text,
				"ground_truth_decl": lean_decl.get("decl", ""),
			},
			f,
			indent=2,
			ensure_ascii=False,
		)

	with open(scratch_path, "w", encoding="utf-8") as f:
		# Reset scratch file to a known starting point before each run.
		f.write(initial_scratch)

	prompt = _build_problem_prompt(theorem_name, scratch_relative, source_file, statement_text, proof_text)
	# Run EPFLemma once for this node using current model config/budgets.
	input_tokens, output_tokens, turns_used, error = _run_agent(
		prompt, project_root, model_cfg, dry_run=dry_run,
		thinking_log_path=os.path.join(output_dir, "thinking_trace.txt"),
	)

	with open(scratch_path, "r", encoding="utf-8") as f:
		# Read back the model-edited scratch file exactly as produced.
		final_scratch = f.read()

	with open(os.path.join(output_dir, "scratch_after.lean"), "w", encoding="utf-8") as f:
		f.write(final_scratch)

	prefix_preserved = final_scratch.startswith(prefix)
	# Guardrail: require model to keep the original prefix untouched.
	# If this fails, downstream checks are treated as invalid for this sample.
	generated_tail = final_scratch[len(prefix):] if prefix_preserved else ""
	# Verify by running lake env lean on the scratch file (full pipeline: compile + sorry check).
	if prefix_preserved and generated_tail.strip():
		lean_verified, no_placeholders_left = _verify_generated_tail(scratch_path, project_root, generated_tail)
	else:
		lean_verified, no_placeholders_left = False, False
	beql = False
	beq_plus = False
	# Run semantic checks only for well-typed generations in unchanged context.
	# This avoids false positives from malformed or context-corrupt outputs.
	if prefix_preserved and lean_verified:
		beql, beq_plus = _compute_beq_checks(prefix, generated_tail, lean_decl, repl_config)

	result = ProblemResult(
		project_name=project_name,
		node_label=str(node["label"]),
		theorem_name=theorem_name,
		source_file=source_file,
		scratch_file=scratch_relative,
		model=str(model_cfg.get("model", "")),
		max_turns=int(model_cfg.get("max_turns", 50)),
		turns_used=turns_used,
		input_tokens=input_tokens,
		output_tokens=output_tokens,
		prefix_preserved=prefix_preserved,
		no_placeholders_left=no_placeholders_left,
		lean_verified=lean_verified,
		beql=beql,
		beq_plus=beq_plus,
		error=error if prefix_preserved else (error or "scratch_prefix_modified"),
		duration_seconds=time.time() - start_time,
	)

	with open(os.path.join(output_dir, "result.json"), "w", encoding="utf-8") as f:
		# Theorem-level metrics consumed by downstream summarization scripts.
		json.dump(dataclasses.asdict(result), f, indent=2)

	return result


def _repo_dirname(git_url: str, commit: str) -> str:
	"""Match RLMEval traced repo directory naming convention."""
	return f"{git_url.split('/')[-1]}_{commit}"


def _repo_matches_filter(repo_filter: str | None, project_name: str, git_url: str) -> bool:
	"""Return True when a repository matches the optional CLI repo filter."""
	if not repo_filter:
		return True
	f = repo_filter.strip().lower()
	if not f:
		return True
	git_repo_name = git_url.rstrip("/").split("/")[-1].replace(".git", "").lower()
	return f in {
		project_name.lower(),
		git_repo_name,
	}


def _load_jsonl(path: str) -> list[dict[str, Any]]:
	"""Load JSONL into memory for deterministic iteration over benchmark data."""
	with jsonlines.open(path) as reader:
		return list(reader)


def _find_latest_run_dir(base_dir: str) -> str | None:
	"""Return the most recently created timestamped run subdirectory, or None."""
	try:
		entries = [
			e for e in os.scandir(base_dir)
			if e.is_dir() and re.match(r"\d{8}_\d{6}$", e.name)
		]
		if not entries:
			return None
		return max(entries, key=lambda e: e.name).path
	except OSError:
		return None


def _load_result_if_done(output_dir: str) -> ProblemResult | None:
	"""Return a ProblemResult loaded from result.json if already completed, else None."""
	result_path = os.path.join(output_dir, "result.json")
	try:
		with open(result_path, "r", encoding="utf-8") as f:
			d = json.load(f)
		return ProblemResult(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(ProblemResult)}})
	except (OSError, json.JSONDecodeError, TypeError):
		return None


def _read_lean_toolchain(project_root: str) -> str | None:
	"""Read the repo's pinned Lean version from its lean-toolchain file."""
	toolchain_path = os.path.join(project_root, "lean-toolchain")
	try:
		with open(toolchain_path, "r", encoding="utf-8") as f:
			return f.read().strip() or None
	except OSError:
		return None


def _bundled_repl_path() -> Path:
	"""Return the REPL cache bundled with lean-interact, if available."""
	return Path(lean_interact.__file__).resolve().parent / "cache" / "augustepoiroux" / "repl" / "repl_clean_copy"

def _iter_eligible_nodes(blueprint_to_lean: list[dict[str, Any]], lean_files: dict[str, str]) -> list[dict[str, Any]]:
	"""Select nodes with NL statement/proof and a single linked theorem declaration."""
	eligible: list[dict[str, Any]] = []
	for node in blueprint_to_lean:
		# Skip blueprint entries that cannot be traced to a stable theorem target.
		if not node.get("label"):
			continue
		# Require both statement and proof text to evaluate full autoformalization.
		if not str(node.get("processed_text") or "").strip():
			continue
		if not str(node.get("proof", {}).get("text") or "").strip():
			continue
		# Keep only declarations backed by a concrete source file and theorem metadata.
		decls = [
			decl
			for decl in node.get("lean_declarations", [])
			if "file" in decl and "start_idx" in decl and "theorem_info" in decl and decl["file"] in lean_files
		]
		# One node -> one declaration keeps evaluation deterministic and comparable.
		if len(decls) != 1:
			continue
		copied = dict(node)
		# Normalize downstream shape so callers can assume exactly one declaration.
		copied["lean_declarations"] = decls
		eligible.append(copied)
	return eligible


def _write_summary(results: list[ProblemResult], output_dir: str, model: str, timestamp: str) -> None:
	"""Write global and per-project aggregate metrics for one model run."""
	by_project: dict[str, list[ProblemResult]] = defaultdict(list)
	for result in results:
		# Bucket theorem outcomes by project for per-repository reporting.
		by_project[result.project_name].append(result)

	total_n = len(results)
	total_verified = sum(1 for r in results if r.lean_verified)
	total_beql = sum(1 for r in results if r.beql)
	total_beq_plus = sum(1 for r in results if r.beq_plus)
	total_input = sum(r.input_tokens for r in results)
	total_output = sum(r.output_tokens for r in results)
	total_turns = sum(r.turns_used for r in results)

	# Global aggregates summarize one full model run across all processed projects.
	summary = {
		"model": model,
		"timestamp": timestamp,
		"n_problems": total_n,
		"n_verified": total_verified,
		"n_beql": total_beql,
		"n_beq_plus": total_beq_plus,
		"pass_rate": (total_verified / total_n) if total_n else 0.0,
		"beql_rate": (total_beql / total_n) if total_n else 0.0,
		"beq_plus_rate": (total_beq_plus / total_n) if total_n else 0.0,
		"total_input_tokens": total_input,
		"total_output_tokens": total_output,
		"avg_turns": (total_turns / total_n) if total_n else 0.0,
		"by_project": {},
	}

	for project_name, project_results in by_project.items():
		n = len(project_results)
		verified = sum(1 for r in project_results if r.lean_verified)
		beql = sum(1 for r in project_results if r.beql)
		beq_plus = sum(1 for r in project_results if r.beq_plus)
		# Per-project breakdown mirrors global fields for easy diffing/export.
		summary["by_project"][project_name] = {
			"n_problems": n,
			"n_verified": verified,
			"n_beql": beql,
			"n_beq_plus": beq_plus,
			"pass_rate": (verified / n) if n else 0.0,
			"beql_rate": (beql / n) if n else 0.0,
			"beq_plus_rate": (beq_plus / n) if n else 0.0,
			"total_input_tokens": sum(r.input_tokens for r in project_results),
			"total_output_tokens": sum(r.output_tokens for r in project_results),
			"avg_turns": (sum(r.turns_used for r in project_results) / n) if n else 0.0,
		}

	summary_dir = os.path.join(output_dir, "summary", model.split("/")[-1], timestamp)
	os.makedirs(summary_dir, exist_ok=True)
	# Keep one canonical summary file per run timestamp.
	# This file is the stable entry point for post-run analysis.
	with open(os.path.join(summary_dir, "global_summary.json"), "w", encoding="utf-8") as f:
		json.dump(summary, f, indent=2)


def run_recheck(
	benchmark_config_path: str,
	model_config_path: str,
	traced_repos_root: str,
	output_root: str,
	repo_filter: str | None = None,
) -> None:
	"""Re-run compilation and BEq checks on all results in the latest run dir.

	For every theorem that has a scratch_after.lean, re-runs the Lean compilation
	to update lean_verified/no_placeholders_left. Then, for all newly or previously
	verified theorems with a known lean_decl, re-runs the BEq checks.
	Reads scratch_after.lean and lean_decl from traced data, then updates result.json
	in-place. Does not call the model.
	"""
	with open(benchmark_config_path, "r", encoding="utf-8") as f:
		benchmark_config = yaml.safe_load(f)
	with open(model_config_path, "r", encoding="utf-8") as f:
		model_cfg = yaml.safe_load(f)
	model = str(model_cfg.get("model", ""))
	model_slug = model.split("/")[-1]

	for repo in benchmark_config.get("repositories", []):
		git_url = str(repo["git_url"])
		commit = str(repo["commit"])
		project_name = str(repo["project_name"])
		if not _repo_matches_filter(repo_filter, project_name, git_url):
			continue

		project_root_dir = os.path.join(traced_repos_root, _repo_dirname(git_url, commit))
		project_root = os.path.join(project_root_dir, git_url.split("/")[-1])
		blueprint_path = os.path.join(project_root_dir, "blueprint_to_lean.jsonl")
		lean_files_path = os.path.join(project_root_dir, "lean_files.jsonl")
		if not os.path.exists(blueprint_path) or not os.path.exists(lean_files_path):
			_log(f"Skipping {project_name}: missing traced benchmark artifacts")
			continue

		# Build label -> lean_decl lookup from blueprint data.
		blueprint_to_lean = _load_jsonl(blueprint_path)
		lean_files_rows = _load_jsonl(lean_files_path)
		lean_files = {row["file"]: row["content"] for row in lean_files_rows}
		decl_by_label: dict[str, dict] = {}
		for node in blueprint_to_lean:
			label = node.get("label")
			if not label:
				continue
			decls = [d for d in node.get("lean_declarations", []) if "theorem_info" in d]
			if len(decls) == 1:
				decl_by_label[label] = decls[0]

		project_base = os.path.join(output_root, project_name, model_slug)
		run_dir = _find_latest_run_dir(project_base)
		if not run_dir:
			_log(f"[{project_name}] No run dir found under {project_base}, skipping.")
			continue
		_log(f"==== {project_name} — rechecking BEq in {run_dir} ====")

		lean_version = _read_lean_toolchain(project_root)
		try:
			repl_config = LeanREPLConfig(project=LocalProject(directory=project_root, auto_build=False))
		except ValueError as exc:
			if "does not match the fetched Lean version" not in str(exc):
				raise
			local_repl_path = _bundled_repl_path()
			if not local_repl_path.exists():
				raise
			repl_config = LeanREPLConfig(
				project=LocalProject(directory=project_root, auto_build=False),
				local_repl_path=local_repl_path,
				build_repl=False,
			)

		updated = skipped = errors = 0
		all_results: list[ProblemResult] = []
		theorem_dirs = sorted(
			e.path for e in os.scandir(run_dir)
			if e.is_dir() and os.path.exists(os.path.join(e.path, "result.json"))
		)
		for theorem_dir in theorem_dirs:
			existing = _load_result_if_done(theorem_dir)
			if existing is None:
				continue

			scratch_after_path = os.path.join(theorem_dir, "scratch_after.lean")
			try:
				final_scratch = open(scratch_after_path, "r", encoding="utf-8").read()
			except OSError:
				# No scratch file — nothing to recheck; keep existing result as-is.
				all_results.append(existing)
				skipped += 1
				continue

			lean_decl = decl_by_label.get(existing.node_label)
			if lean_decl is None:
				_log(f"  [warn] No lean_decl found for {existing.node_label}, skipping recheck.")
				all_results.append(existing)
				skipped += 1
				continue

			prefix = lean_files[lean_decl["file"]][:int(lean_decl["start_idx"])]
			if not final_scratch.startswith(prefix):
				_log(f"  [warn] Prefix not preserved in scratch for {existing.node_label}, skipping.")
				all_results.append(existing)
				skipped += 1
				continue
			generated_tail = final_scratch[len(prefix):]

			# Step 1: re-check compilation.
			_log(f"  Rechecking compilation: {existing.node_label} ...")
			new_verified, new_no_placeholders = _verify_generated_tail(
				scratch_after_path, project_root, generated_tail
			)
			compile_changed = (new_verified != existing.lean_verified) or (
				new_no_placeholders != existing.no_placeholders_left
			)
			existing.lean_verified = new_verified
			existing.no_placeholders_left = new_no_placeholders
			compile_flag = " [CHANGED]" if compile_changed else ""
			_log(f"    lean_verified={int(new_verified)}{compile_flag}")

			# Step 2: re-check BEq (only when compiled successfully).
			if new_verified:
				_log(f"  Rechecking BEq: {existing.node_label} ...")
				new_beql, new_beq_plus = _compute_beq_checks(prefix, generated_tail, lean_decl, repl_config)
				beq_changed = (new_beql != existing.beql) or (new_beq_plus != existing.beq_plus)
				existing.beql = new_beql
				existing.beq_plus = new_beq_plus
				beq_flag = " [CHANGED]" if beq_changed else ""
				_log(f"    BEqL={int(new_beql)} BEq+={int(new_beq_plus)}{beq_flag}")
			else:
				# Reset BEq fields for unverified results.
				existing.beql = False
				existing.beq_plus = False

			# Persist updated result.
			result_path = os.path.join(theorem_dir, "result.json")
			with open(result_path, "w", encoding="utf-8") as f:
				json.dump(dataclasses.asdict(existing), f, indent=2)
			all_results.append(existing)
			updated += 1

		_log(f"  Done: {updated} rechecked, {skipped} skipped (no scratch/no decl), {errors} errors.")
		run_timestamp = os.path.basename(run_dir)
		_write_summary(all_results, output_root, model, run_timestamp)
		_log(f"  Summary updated for {run_dir}")


def run_evaluation(
	benchmark_config_path: str,
	model_config_path: str,
	traced_repos_root: str,
	output_root: str,
	max_theorems: int | None = None,
	repo_filter: str | None = None,
	dry_run: bool = False,
	resume: bool = False,
) -> None:
	"""Run evaluation across all repositories listed in the benchmark config."""
	with open(benchmark_config_path, "r", encoding="utf-8") as f:
		benchmark_config = yaml.safe_load(f)
	with open(model_config_path, "r", encoding="utf-8") as f:
		model_cfg = yaml.safe_load(f)

	# If api_key is omitted in the model config, fall back to environment variables.
	# This avoids storing secrets in YAML files and removes the need for runtime key-injection files.
	if not str(model_cfg.get("api_key", "") or "").strip():
		model_cfg["api_key"] = (
			os.getenv("EPFLEMMA_OPENAI_API_KEY")
			or os.getenv("OPENAI_API_KEY")
			or ""
		)

	if not os.path.isdir(traced_repos_root):
		raise SystemExit(
			f"Traced repos directory not found at {traced_repos_root}. Run RLMEval benchmark extraction first."
		)

	timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	model = str(model_cfg.get("model", "claude-sonnet-4-20250514"))
	# Theorem cap precedence: explicit CLI arg > model config fallback.
	if max_theorems is None:
		max_problems = model_cfg.get("max_problems")
		max_theorems_int = int(max_problems) if max_problems is not None else None
	else:
		max_theorems_int = max(0, int(max_theorems))
	all_results: list[ProblemResult] = []

	for repo in benchmark_config.get("repositories", []):
		git_url = str(repo["git_url"])
		commit = str(repo["commit"])
		project_name = str(repo["project_name"])

		# Optional filter to evaluate only one selected benchmark repository.
		if not _repo_matches_filter(repo_filter, project_name, git_url):
			continue

		project_root_dir = os.path.join(traced_repos_root, _repo_dirname(git_url, commit))
		project_root = os.path.join(project_root_dir, git_url.split("/")[-1])
		blueprint_path = os.path.join(project_root_dir, "blueprint_to_lean.jsonl")
		lean_files_path = os.path.join(project_root_dir, "lean_files.jsonl")

		if not os.path.exists(blueprint_path) or not os.path.exists(lean_files_path):
			# Skip repositories not yet extracted into traced artifacts.
			_log(f"Skipping {project_name}: missing traced benchmark artifacts")
			continue

		_log(f"==== {project_name} ====")
		blueprint_to_lean = _load_jsonl(blueprint_path)
		lean_files_rows = _load_jsonl(lean_files_path)
		# Keep an in-memory map for quick source-file lookup during per-node evaluation.
		lean_files = {row["file"]: row["content"] for row in lean_files_rows}
		# Keep only benchmark nodes that map cleanly to one theorem declaration.
		eligible_nodes = _iter_eligible_nodes(blueprint_to_lean, lean_files)
		if max_theorems_int is not None:
			# Optional cap for smoke tests / budget-limited runs.
			# Applies to the first n eligible nodes in each repository.
			eligible_nodes = eligible_nodes[:max_theorems_int]

		lean_version = _read_lean_toolchain(project_root)
		try:
			repl_config = LeanREPLConfig(
				project=LocalProject(directory=project_root, auto_build=False),
			)
		except ValueError as exc:
			# Older traced repos can be pinned to Lean versions that the Git-based REPL
			# cache cannot resolve. Fall back to the bundled local REPL cache so the
			# benchmark can still run instead of aborting before evaluation starts.
			if "does not match the fetched Lean version" not in str(exc):
				raise
			local_repl_path = _bundled_repl_path()
			if not local_repl_path.exists():
				raise
			_log(
				f"[warn] Falling back to bundled local REPL cache at {local_repl_path} for {project_name} "
				f"(project Lean {lean_version or 'unknown'})."
			)
			repl_config = LeanREPLConfig(
				project=LocalProject(directory=project_root, auto_build=False),
				local_repl_path=local_repl_path,
				build_repl=False,
			)
		# Repl config is shared across theorem checks within this project.
		model_slug = model.split("/")[-1]
		project_base = os.path.join(output_root, project_name, model_slug)
		if resume:
			# Try to reuse the most recent existing run dir for this project+model.
			prior_dir = _find_latest_run_dir(project_base)
			if prior_dir:
				project_output_root = prior_dir
				run_timestamp = os.path.basename(prior_dir)
				_log(f"[resume] Continuing from existing run dir: {project_output_root}")
			else:
				project_output_root = os.path.join(project_base, timestamp)
				run_timestamp = timestamp
				_log(f"[resume] No prior run found; starting fresh.")
		else:
			project_output_root = os.path.join(project_base, timestamp)
			run_timestamp = timestamp
		# Project-level output bundles model + benchmark config snapshots for reproducibility.
		os.makedirs(project_output_root, exist_ok=True)

		# Lockfile: prevent two concurrent runs from writing to the same output dir.
		lock_path = os.path.join(project_output_root, "RUNNING.lock")
		if os.path.exists(lock_path):
			try:
				with open(lock_path) as _lf:
					existing_pid = int(_lf.read().strip())
				try:
					os.kill(existing_pid, 0)  # signal 0: just check existence
					raise SystemExit(
						f"[error] Another evaluation is already running on {project_output_root} "
						f"(PID {existing_pid}). Kill it first or delete {lock_path}."
					)
				except ProcessLookupError:
					pass  # Stale lock from a previous crash — safe to overwrite.
			except (ValueError, OSError):
				pass
		with open(lock_path, "w") as _lf:
			_lf.write(str(os.getpid()))
		import atexit
		atexit.register(lambda p=lock_path: os.path.exists(p) and os.unlink(p))

		with open(os.path.join(project_output_root, "benchmark_config.yaml"), "w", encoding="utf-8") as f:
			yaml.safe_dump(benchmark_config, f)
		with open(os.path.join(project_output_root, "model_config.yaml"), "w", encoding="utf-8") as f:
			yaml.safe_dump(model_cfg, f)

		for idx, node in enumerate(eligible_nodes, start=1):
			# Each node is evaluated independently in its own scratch artifact folder.
			# Failures on one node do not interrupt evaluation of remaining nodes.
			theorem_output_dir = os.path.join(project_output_root, _sanitize_filename(str(node["label"])))
			if resume:
				existing = _load_result_if_done(theorem_output_dir)
				if existing is not None:
					all_results.append(existing)
					status = "OK" if existing.lean_verified else "FAIL"
					_log(
						f"[{project_name} {idx}/{len(eligible_nodes)}] {status} {existing.node_label} "
						f"turns={existing.turns_used} in={existing.input_tokens:,} out={existing.output_tokens:,} "
						f"BEqL={int(existing.beql)} BEq+={int(existing.beq_plus)}"
						+ (f" err={existing.error}" if existing.error else "")
						+ " [skipped, already done]"
					)
					continue
			result = evaluate_problem(
				node=node,
				project_name=project_name,
				project_root=project_root,
				original_file_content=lean_files[node["lean_declarations"][0]["file"]],
				output_dir=theorem_output_dir,
				model_cfg=model_cfg,
				repl_config=repl_config,
				dry_run=dry_run,
			)
			all_results.append(result)
			# Lightweight progress line for long runs across many repositories.
			status = "OK" if result.lean_verified else "FAIL"
			_log(
				f"[{project_name} {idx}/{len(eligible_nodes)}] {status} {result.node_label} "
				f"turns={result.turns_used} in={result.input_tokens:,} out={result.output_tokens:,} "
				f"BEqL={int(result.beql)} BEq+={int(result.beq_plus)}"
				+ (f" err={result.error}" if result.error else "")
			)

	_write_summary(all_results, output_root, model, run_timestamp)
	# Single summary for this model/timestamp across all processed projects.
	_log(f"Summary written under {os.path.join(output_root, 'summary', model.split('/')[-1], run_timestamp)}")


def main() -> None:
	"""Parse CLI arguments and launch the full autoformalization evaluation."""
	parser = argparse.ArgumentParser(description="Evaluate EPFLemma on RLM25 full autoformalization problems")
	parser.add_argument(
		"--benchmark-config",
		required=True,
		help="Path to RLMEval benchmark config YAML, e.g. RLMEval/configs/benchmark/rlm25.yaml",
	)
	parser.add_argument(
		"--model-config",
		required=True,
		help="Path to external model config YAML",
	)
	parser.add_argument(
		"--traced-repos-root",
		default=str(DEFAULT_TRACED_REPOS_ROOT),
		help="Directory containing RLMEval traced benchmark repos",
	)
	parser.add_argument(
		"--output-dir",
		default=str(DEFAULT_OUTPUT_ROOT),
		help="Workspace-level output directory",
	)
	parser.add_argument(
		"--max-theorems",
		type=int,
		default=None,
		help="Run only the first n eligible theorems per repository (overrides model config max_problems)",
	)
	parser.add_argument(
		"--repo",
		default=None,
		help="Evaluate only one repository by project name or git repo name (default: all repositories)",
	)
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="Skip EPFLemma call; sleep 30s per theorem and continue as if no edits were produced",
	)
	parser.add_argument(
		"--resume",
		action="store_true",
		help="Resume the most recent interrupted run: skip theorems that already have a result.json",
	)
	parser.add_argument(
		"--recheck",
		action="store_true",
		help="Re-run compilation and BEq checks on all results in the latest run dir, without calling the model",
	)
	args = parser.parse_args()

	if args.recheck:
		run_recheck(
			benchmark_config_path=args.benchmark_config,
			model_config_path=args.model_config,
			traced_repos_root=args.traced_repos_root,
			output_root=args.output_dir,
			repo_filter=args.repo,
		)
		return

	run_evaluation(
		benchmark_config_path=args.benchmark_config,
		model_config_path=args.model_config,
		traced_repos_root=args.traced_repos_root,
		output_root=args.output_dir,
		max_theorems=args.max_theorems,
		repo_filter=args.repo,
		dry_run=args.dry_run,
		resume=args.resume,
	)


if __name__ == "__main__":
	main()
