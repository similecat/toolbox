"""Post-chapter active-recall quiz generator & grader (Copilot CLI + OpenAI-compatible HTTP LLM)."""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Callable, Dict, List, Optional

import requests

MAX_CHAPTER_PROMPT_CHARS = 32_000


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def extract_json_object_string(raw_text: str) -> str:
    trimmed = raw_text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", trimmed, flags=re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else trimmed
    first_brace = candidate.find("{")
    last_brace = candidate.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        return candidate[first_brace:last_brace + 1]
    return candidate


def budget_chapter_text(plain_text: str, max_chars: int = MAX_CHAPTER_PROMPT_CHARS) -> str:
    if len(plain_text) <= max_chars:
        return plain_text

    lines = plain_text.split("\n")
    headings = [ln for ln in lines if ln.startswith("## ")][:30]
    heading_outline = (
        f"Chapter Section Outline:\n" + "\n".join(headings) + "\n\n" if headings else ""
    )

    remaining_budget = max(4000, max_chars - len(heading_outline) - 200)
    segment_count = 4
    per_segment = remaining_budget // segment_count
    segments: List[str] = []
    for i in range(segment_count):
        ratio = i / (segment_count - 1)
        max_start = max(0, len(plain_text) - per_segment)
        start = int(max_start * ratio)
        segments.append(plain_text[start:start + per_segment].strip())

    return heading_outline + "\n\n[...]\n\n".join(segments)


def _validate_generated_quiz_payload(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Quiz response must be a JSON object")
    language = str(data.get("language") or "en").strip()
    questions = data.get("questions")
    if not isinstance(questions, list) or not (2 <= len(questions) <= 10):
        raise ValueError("questions must be an array of 2..10 items")

    validated_questions: List[Dict[str, Any]] = []
    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            raise ValueError(f"question {idx} must be an object")
        q_type = q.get("type")
        prompt = str(q.get("prompt") or "").strip()
        if len(prompt) < 5:
            raise ValueError(f"question {idx} prompt too short")
        if q_type == "mcq":
            options = q.get("options")
            if not isinstance(options, list) or len(options) != 4 or any(not str(o).strip() for o in options):
                raise ValueError(f"mcq {idx} must have 4 non-empty options")
            correct_idx = q.get("correctIndex")
            if correct_idx not in (0, 1, 2, 3):
                raise ValueError(f"mcq {idx} correctIndex must be 0..3")
            explanation = str(q.get("explanation") or "").strip()
            if len(explanation) < 5:
                raise ValueError(f"mcq {idx} explanation too short")
            item: Dict[str, Any] = {
                "id": f"q-{idx + 1}",
                "type": "mcq",
                "prompt": prompt,
                "options": [str(o).strip() for o in options],
                "correctIndex": int(correct_idx),
                "explanation": explanation,
            }
            if q.get("sourceExcerpt"):
                item["sourceExcerpt"] = str(q["sourceExcerpt"]).strip()
            validated_questions.append(item)
        elif q_type == "short_answer":
            rubric = q.get("rubricKeyPoints")
            if not isinstance(rubric, list) or not (1 <= len(rubric) <= 8):
                raise ValueError(f"short_answer {idx} rubricKeyPoints must have 1..8 items")
            sample = str(q.get("sampleAnswer") or "").strip()
            if len(sample) < 5:
                raise ValueError(f"short_answer {idx} sampleAnswer too short")
            validated_questions.append(
                {
                    "id": f"q-{idx + 1}",
                    "type": "short_answer",
                    "prompt": prompt,
                    "rubricKeyPoints": [str(r).strip() for r in rubric if str(r).strip()],
                    "sampleAnswer": sample,
                }
            )
        else:
            raise ValueError(f"Unsupported question type: {q_type}")

    return {"language": language, "questions": validated_questions}


def _validate_short_answer_grading_batch(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Grading response must be a JSON object")
    evals = data.get("evaluations")
    summary = str(data.get("summaryTakeaway") or "").strip()
    if not isinstance(evals, list) or len(summary) < 5:
        raise ValueError("Grading batch requires evaluations list and summaryTakeaway")

    cleaned_evals: List[Dict[str, Any]] = []
    for item in evals:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "needs_review")
        if verdict not in ("strong", "partial", "needs_review"):
            verdict = "partial"
        score_pct = max(0, min(100, round(float(item.get("scorePct", 0)))))
        cleaned_evals.append(
            {
                "questionId": str(item.get("questionId") or ""),
                "scorePct": score_pct,
                "verdict": verdict,
                "feedback": str(item.get("feedback") or "Reviewed."),
                "missedPoints": [str(m) for m in (item.get("missedPoints") or [])],
            }
        )
    return {"evaluations": cleaned_evals, "summaryTakeaway": summary}


def _resolve_copilot_cli_path() -> Optional[str]:
    explicit = (os.environ.get("COPILOT_CLI_PATH") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    return shutil.which("copilot")


def _resolve_http_llm_config() -> Optional[Dict[str, str]]:
    api_key = (
        os.environ.get("TOOLBOX_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GITHUB_MODELS_TOKEN")
        or ""
    ).strip()
    if not api_key:
        return None
    base_url = (
        os.environ.get("TOOLBOX_LLM_BASE_URL")
        or (
            "https://models.inference.ai.azure.com"
            if os.environ.get("GITHUB_MODELS_TOKEN")
            else "https://api.openai.com/v1"
        )
    ).rstrip("/")
    model = (os.environ.get("TOOLBOX_LLM_MODEL") or "gpt-4o-mini").strip()
    return {"api_key": api_key, "base_url": base_url, "model": model}


class CopilotQuizService:
    """Generates and grades chapter quizzes via GitHub Copilot CLI or configured HTTP LLM API."""

    def get_status(self) -> Dict[str, Any]:
        http_cfg = _resolve_http_llm_config()
        if http_cfg:
            return {
                "available": True,
                "cliPath": f"http-llm:{http_cfg['model']}",
            }

        cli_path = _resolve_copilot_cli_path()
        if cli_path:
            return {
                "available": True,
                "cliPath": cli_path,
            }

        return {
            "available": False,
            "reason": (
                "Install/authenticate GitHub Copilot CLI (`copilot`) or set "
                "TOOLBOX_LLM_API_KEY / OPENAI_API_KEY on the server to enable AI quizzes."
            ),
        }

    def _invoke_model(self, system_prompt: str, user_prompt: str, timeout_sec: int = 75) -> str:
        http_cfg = _resolve_http_llm_config()
        if http_cfg:
            endpoint = f"{http_cfg['base_url']}/chat/completions"
            resp = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {http_cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": http_cfg["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.3,
                },
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
            return str(payload["choices"][0]["message"]["content"] or "")

        cli_path = _resolve_copilot_cli_path()
        if not cli_path:
            raise RuntimeError(
                "AI quiz backend is not configured on this server (missing `copilot` CLI or TOOLBOX_LLM_API_KEY)."
            )

        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
        with tempfile.TemporaryDirectory(prefix="epub-quiz-") as empty_dir:
            proc = subprocess.run(
                [
                    cli_path,
                    "-p",
                    "Read the instructions and chapter text from stdin and output ONLY the requested JSON object.",
                    "-s",
                    "--no-color",
                    "--no-custom-instructions",
                    "--disable-builtin-mcps",
                    "--no-ask-user",
                ],
                input=combined_prompt,
                text=True,
                encoding="utf-8",
                cwd=empty_dir,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
            if proc.returncode != 0:
                stderr_msg = (proc.stderr or "").strip() or f"Copilot CLI exited with status {proc.returncode}"
                raise RuntimeError(stderr_msg)
            return proc.stdout or ""

    def _run_structured_turn(
        self,
        system_prompt: str,
        user_prompt: str,
        validator: Callable[[Any], Dict[str, Any]],
    ) -> Dict[str, Any]:
        first_raw = self._invoke_model(system_prompt, user_prompt)
        if not first_raw.strip():
            raise RuntimeError("AI model returned an empty response")
        try:
            return validator(json.loads(extract_json_object_string(first_raw)))
        except Exception as first_err:
            repair_prompt = (
                f"{user_prompt}\n\nYour previous output failed JSON validation ({first_err}). "
                "Output ONLY the corrected valid JSON object with no markdown fences."
            )
            second_raw = self._invoke_model(system_prompt, repair_prompt)
            return validator(json.loads(extract_json_object_string(second_raw)))

    def generate_chapter_quiz(
        self,
        *,
        spine_index: int,
        chapter_title: str,
        chapter_plain_text: str,
        language: str,
        chapter_annotations: Optional[List[Dict[str, Any]]] = None,
        mcq_count: int = 4,
        short_answer_count: int = 2,
    ) -> Dict[str, Any]:
        trimmed_text = chapter_plain_text.strip()
        if len(trimmed_text) < 40:
            raise ValueError("This chapter has too little text content to generate a meaningful quiz.")

        budgeted = budget_chapter_text(trimmed_text)
        notes_block = ""
        if chapter_annotations:
            formatted = []
            for idx, ann in enumerate(chapter_annotations[:8], start=1):
                quote_txt = str(ann.get("selectedText", ""))[:280]
                note_txt = str(ann.get("noteMarkdown", ""))[:200]
                note_part = f' | Reader Note: "{note_txt}"' if note_txt else ""
                formatted.append(f'{idx}. Quote: "{quote_txt}"{note_part}')
            notes_block = (
                "\n\nReader's Highlighted Passages & Notes in This Chapter "
                "(prioritize testing deep understanding of these concepts):\n"
                + "\n".join(formatted)
            )

        system_prompt = (
            "You are an expert active-recall reading tutor. "
            "Generate rigorous, insightful comprehension questions strictly grounded in the supplied chapter text. "
            "Never invent facts outside the chapter text. "
            "Respond ONLY with a single valid JSON object matching the requested schema—no markdown fences or extra commentary."
        )

        user_prompt = f"""Generate a post-chapter comprehension quiz in the SAME language as the chapter text (language hint: "{language}").

Chapter Heading: {chapter_title}

Requirements:
1. Include EXACTLY {mcq_count} multiple-choice questions ("type": "mcq") and EXACTLY {short_answer_count} short-answer questions ("type": "short_answer").
2. Focus on core mechanisms, trade-offs, cause-and-effect reasoning, and common misconceptions—avoid trivial memorization.
3. Every MCQ must have exactly 4 plausible options, a 0-based "correctIndex" (0, 1, 2, or 3), and a clear "explanation".
4. Every short-answer question must include 2 to 5 concise "rubricKeyPoints" and a concise "sampleAnswer".

Required JSON Schema:
{{
  "language": "string (e.g. zh-CN or en)",
  "questions": [
    {{
      "id": "q1",
      "type": "mcq",
      "prompt": "question text",
      "options": ["option A", "option B", "option C", "option D"],
      "correctIndex": 0,
      "explanation": "why option A is correct",
      "sourceExcerpt": "short quote from chapter"
    }},
    {{
      "id": "q2",
      "type": "short_answer",
      "prompt": "open-ended question text",
      "rubricKeyPoints": ["key point 1", "key point 2"],
      "sampleAnswer": "concise ideal answer"
    }}
  ]
}}{notes_block}

<chapter_text>
{budgeted}
</chapter_text>"""

        parsed = self._run_structured_turn(
            system_prompt, user_prompt, _validate_generated_quiz_payload
        )
        return {
            "id": str(uuid.uuid4()),
            "spineIndex": spine_index,
            "chapterTitle": chapter_title,
            "language": parsed["language"] or language,
            "createdAt": _utc_iso_now(),
            "questions": parsed["questions"],
            "results": {},
        }

    def grade_quiz_submission(
        self,
        quiz: Dict[str, Any],
        answers: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        short_to_grade: List[Dict[str, Any]] = []
        quiz_lang = str(quiz.get("language") or "en")

        for q in quiz.get("questions", []):
            qid = q["id"]
            submitted = answers.get(qid) or {}
            if q["type"] == "mcq":
                sel_idx = int(submitted.get("selectedIndex", -1)) if submitted.get("type") == "mcq" else -1
                is_correct = sel_idx == q["correctIndex"]
                results[qid] = {
                    "type": "mcq",
                    "selectedIndex": sel_idx,
                    "isCorrect": is_correct,
                    "scorePct": 100 if is_correct else 0,
                }
            else:
                ans_text = (
                    str(submitted.get("answerText") or "").strip()
                    if submitted.get("type") == "short_answer"
                    else ""
                )
                if not ans_text:
                    results[qid] = {
                        "type": "short_answer",
                        "answerText": "",
                        "scorePct": 0,
                        "verdict": "needs_review",
                        "feedback": (
                            "未作答。建议结合参考答案回顾以下关键要点。"
                            if quiz_lang.startswith("zh")
                            else "No answer submitted. Review the key points and sample answer below."
                        ),
                        "missedPoints": q.get("rubricKeyPoints", []),
                    }
                else:
                    short_to_grade.append({"question": q, "answerText": ans_text})

        summary_takeaway: Optional[str] = None
        if short_to_grade:
            system_prompt = (
                "You are a fair, constructive technical reading coach. "
                "Evaluate the reader's short-answer responses strictly against each question's rubricKeyPoints and sampleAnswer. "
                "Write all feedback and summaryTakeaway in the quiz language. "
                "Respond ONLY with a valid JSON object matching the schema."
            )
            items_payload = [
                {
                    "questionId": item["question"]["id"],
                    "prompt": item["question"]["prompt"],
                    "rubricKeyPoints": item["question"]["rubricKeyPoints"],
                    "sampleAnswer": item["question"]["sampleAnswer"],
                    "readerAnswer": item["answerText"],
                }
                for item in short_to_grade
            ]
            user_prompt = f"""Quiz Language: {quiz_lang}
Chapter: {quiz['chapterTitle']}

Evaluate each response below and return JSON matching:
{{
  "evaluations": [
    {{
      "questionId": "q-...",
      "scorePct": 85,
      "verdict": "strong" | "partial" | "needs_review",
      "feedback": "1-3 sentences of constructive feedback",
      "missedPoints": ["any rubric points missed"]
    }}
  ],
  "summaryTakeaway": "1-2 sentence chapter mastery takeaway"
}}

Submissions to grade:
{json.dumps(items_payload, ensure_ascii=False, indent=2)}"""

            graded_batch = self._run_structured_turn(
                system_prompt, user_prompt, _validate_short_answer_grading_batch
            )
            summary_takeaway = graded_batch["summaryTakeaway"]
            by_id = {ev["questionId"]: ev for ev in graded_batch["evaluations"]}

            for item in short_to_grade:
                qid = item["question"]["id"]
                ev = by_id.get(qid)
                if ev:
                    results[qid] = {
                        "type": "short_answer",
                        "answerText": item["answerText"],
                        "scorePct": ev["scorePct"],
                        "verdict": ev["verdict"],
                        "feedback": ev["feedback"],
                        "missedPoints": ev["missedPoints"],
                    }
                else:
                    results[qid] = {
                        "type": "short_answer",
                        "answerText": item["answerText"],
                        "scorePct": 50,
                        "verdict": "partial",
                        "feedback": "Compare your response with the reference answer below.",
                        "missedPoints": [],
                    }

        q_list = quiz.get("questions", [])
        total_score = sum(float(results.get(q["id"], {}).get("scorePct", 0)) for q in q_list)
        overall_pct = round(total_score / len(q_list)) if q_list else 0

        if not summary_takeaway:
            if quiz_lang.startswith("zh"):
                summary_takeaway = (
                    "本章核心概念掌握扎实，可继续下一章！"
                    if overall_pct >= 80
                    else "建议结合题目解析快速回顾本章重点段落后再继续阅读。"
                )
            else:
                summary_takeaway = (
                    "Strong grasp of this chapter's core ideas—ready for the next chapter!"
                    if overall_pct >= 80
                    else "Review the explanations for missed questions to solidify key concepts."
                )

        updated = dict(quiz)
        updated["completedAt"] = _utc_iso_now()
        updated["overallScorePct"] = overall_pct
        updated["summaryTakeaway"] = summary_takeaway
        updated["results"] = results
        return updated
