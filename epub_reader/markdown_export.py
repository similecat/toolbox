"""Markdown study notebook and quiz history exporter."""
from __future__ import annotations

import datetime
from typing import Any, Dict, List


def build_study_markdown(
    book: Dict[str, Any],
    spine: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> str:
    export_date = datetime.date.today().isoformat()
    lines: List[str] = []

    completed_chapters = state.get("progress", {}).get("completedChapters", [])
    bookmarks = state.get("bookmarks", [])
    annotations = state.get("annotations", [])
    quizzes = state.get("quizzes", [])
    completed_quizzes = [q for q in quizzes if q.get("completedAt")]

    lines.append(f"# {book['title']} — Study Notes & Quiz Log")
    lines.append("")
    lines.append("Status: draft")
    lines.append(f"Date: {export_date}")
    lines.append(f"Source: {book['title']} ({book['author']})")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Author**: {book['author']}")
    lines.append(f"- **Language**: {book['language']}")
    lines.append(f"- **Completed Chapters**: {len(completed_chapters)} / {book['chapterCount']}")
    lines.append(f"- **Bookmarks**: {len(bookmarks)}")
    lines.append(f"- **Highlights & Notes**: {len(annotations)}")
    lines.append(f"- **Completed Quizzes**: {len(completed_quizzes)}")
    lines.append("")

    if bookmarks:
        lines.append("## Bookmarks")
        lines.append("")
        sorted_bookmarks = sorted(
            bookmarks,
            key=lambda b: (b.get("spineIndex", 0), b.get("scrollRatio", 0.0)),
        )
        for bm in sorted_bookmarks:
            pct = round(float(bm.get("scrollRatio", 0.0)) * 100)
            snippet = " ".join(str(bm.get("snippet", "")).split())
            snippet_part = f' — _"{snippet}"_' if snippet else ""
            lines.append(f"- **[{bm['chapterTitle']}] {bm['label']}** ({pct}%){snippet_part}")
        lines.append("")

    if annotations:
        lines.append("## Highlights & Notes")
        lines.append("")
        by_chapter: Dict[int, List[Dict[str, Any]]] = {}
        for ann in annotations:
            by_chapter.setdefault(int(ann["spineIndex"]), []).append(ann)

        for idx in sorted(by_chapter.keys()):
            chapter_anns = sorted(by_chapter[idx], key=lambda a: int(a.get("startOffset", 0)))
            heading = (
                spine[idx]["title"]
                if 0 <= idx < len(spine)
                else chapter_anns[0].get("chapterTitle") or f"Chapter {idx + 1}"
            )
            lines.append(f"### {heading}")
            lines.append("")
            for ann in chapter_anns:
                quoted = str(ann.get("selectedText", "")).replace("\n", "\n> ")
                lines.append(f"> {quoted}")
                lines.append("")
                note_md = str(ann.get("noteMarkdown", "")).strip()
                if note_md:
                    lines.append(note_md)
                    lines.append("")

    if completed_quizzes:
        lines.append("## Chapter Quizzes & Active Recall")
        lines.append("")
        sorted_quizzes = sorted(
            completed_quizzes,
            key=lambda q: (int(q.get("spineIndex", 0)), str(q.get("createdAt", ""))),
        )
        for quiz in sorted_quizzes:
            completed_date = str(quiz.get("completedAt", ""))[:10]
            score = quiz.get("overallScorePct", 0)
            lines.append(f"### {quiz['chapterTitle']} — Score: {score}% ({completed_date})")
            lines.append("")
            if quiz.get("summaryTakeaway"):
                lines.append(f"**Takeaway**: {quiz['summaryTakeaway']}")
                lines.append("")
            results = quiz.get("results", {})
            for q_idx, q in enumerate(quiz.get("questions", []), start=1):
                res = results.get(q["id"])
                lines.append(f"#### Q{q_idx}. {q['prompt']}")
                lines.append("")
                if q["type"] == "mcq":
                    sel_idx = res.get("selectedIndex", -1) if isinstance(res, dict) else -1
                    options = q.get("options", [])
                    picked = options[sel_idx] if 0 <= sel_idx < len(options) else "(No answer)"
                    right = options[q["correctIndex"]]
                    is_correct = bool(isinstance(res, dict) and res.get("isCorrect"))
                    badge = "Correct" if is_correct else "Incorrect"
                    lines.append(f"- **Your Answer ({badge})**: {picked}")
                    lines.append(f"- **Correct Answer**: {right}")
                    lines.append(f"- **Explanation**: {q['explanation']}")
                else:
                    ans_text = (
                        res.get("answerText") if isinstance(res, dict) and res.get("answerText") else "(No answer)"
                    )
                    lines.append(f"- **Your Response**: {ans_text}")
                    if isinstance(res, dict) and res.get("type") == "short_answer":
                        lines.append(f"- **Score**: {res.get('scorePct', 0)}% ({res.get('verdict', '')})")
                        lines.append(f"- **Copilot Feedback**: {res.get('feedback', '')}")
                        missed = res.get("missedPoints") or []
                        if missed:
                            lines.append(f"- **Missed Key Points**: {'; '.join(missed)}")
                    lines.append(f"- **Reference Answer**: {q['sampleAnswer']}")
                lines.append("")

    return "\n".join(lines)
