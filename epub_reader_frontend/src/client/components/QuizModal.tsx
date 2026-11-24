import React, { useState } from 'react';
import { readerApi } from '../api.js';
import type {
  ChapterQuizRecord,
  CopilotStatusResponse,
  ReadingProgress,
} from '../../shared/types.js';

interface QuizModalProps {
  bookId: string;
  spineIndex: number;
  chapterTitle: string;
  initialQuiz: ChapterQuizRecord | null;
  copilotStatus: CopilotStatusResponse | null;
  hasNextChapter: boolean;
  onQuizSaved: (quiz: ChapterQuizRecord, updatedProgress?: ReadingProgress) => void;
  onNextChapter: () => void;
  onClose: () => void;
}

export function QuizModal({
  bookId,
  spineIndex,
  chapterTitle,
  initialQuiz,
  copilotStatus,
  hasNextChapter,
  onQuizSaved,
  onNextChapter,
  onClose,
}: QuizModalProps) {
  const [quiz, setQuiz] = useState<ChapterQuizRecord | null>(initialQuiz);
  const [mcqCount, setMcqCount] = useState(4);
  const [shortCount, setShortCount] = useState(2);
  const [mcqSelections, setMcqSelections] = useState<Record<string, number>>({});
  const [shortAnswers, setShortAnswers] = useState<Record<string, string>>({});
  const [busyState, setBusyState] = useState<'generating' | 'grading' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setError(null);
    setBusyState('generating');
    try {
      const generated = await readerApi.generateChapterQuiz(bookId, spineIndex, {
        mcqCount,
        shortAnswerCount: shortCount,
      });
      setQuiz(generated);
      setMcqSelections({});
      setShortAnswers({});
      onQuizSaved(generated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate quiz with Copilot SDK');
    } finally {
      setBusyState(null);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!quiz) return;
    setError(null);
    setBusyState('grading');
    try {
      const payload: Record<
        string,
        { type: 'mcq'; selectedIndex: number } | { type: 'short_answer'; answerText: string }
      > = {};
      for (const q of quiz.questions) {
        if (q.type === 'mcq') {
          payload[q.id] = { type: 'mcq', selectedIndex: mcqSelections[q.id] ?? -1 };
        } else {
          payload[q.id] = { type: 'short_answer', answerText: shortAnswers[q.id] ?? '' };
        }
      }
      const { quiz: graded, progress } = await readerApi.submitQuiz(bookId, quiz.id, payload);
      setQuiz(graded);
      onQuizSaved(graded, progress);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to evaluate quiz');
    } finally {
      setBusyState(null);
    }
  };

  const isCompleted = Boolean(quiz?.completedAt);

  return (
    <div className="modal-backdrop">
      <div className="modal-panel">
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: '1rem',
            marginBottom: '1.1rem',
            paddingBottom: '0.9rem',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div>
            <span className="badge badge-success" style={{ marginBottom: '0.35rem' }}>
              GitHub Copilot SDK Active Recall
            </span>
            <h2 style={{ margin: 0, fontSize: '1.25rem' }}>{chapterTitle}</h2>
          </div>
          <button type="button" className="btn" onClick={onClose} disabled={Boolean(busyState)}>
            Close
          </button>
        </div>

        {error && (
          <div
            role="alert"
            style={{
              padding: '0.75rem 0.9rem',
              borderRadius: '8px',
              background: 'rgba(220, 38, 38, 0.12)',
              color: 'var(--danger)',
              fontSize: '0.88rem',
              marginBottom: '1rem',
            }}
          >
            {error}
          </div>
        )}

        {/* Step A: Configure & Generate */}
        {!quiz && (
          <div>
            <p style={{ margin: '0 0 1rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
              Copilot SDK will read this chapter in an isolated, tool-less ephemeral session and
              craft comprehension questions (incorporating any passages you highlighted or noted).
            </p>

            {copilotStatus && !copilotStatus.available && (
              <div
                style={{
                  padding: '0.75rem 0.9rem',
                  borderRadius: '8px',
                  background: 'rgba(245, 158, 11, 0.14)',
                  marginBottom: '1rem',
                  fontSize: '0.86rem',
                }}
              >
                <strong>Copilot SDK Notice:</strong> {copilotStatus.reason}
              </div>
            )}

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: '1fr 1fr',
                gap: '1rem',
                marginBottom: '1.25rem',
              }}
            >
              <div>
                <label className="field-label" htmlFor="quiz-mcq-count">
                  Multiple-Choice Questions
                </label>
                <select
                  id="quiz-mcq-count"
                  className="text-input"
                  value={mcqCount}
                  onChange={(e) => setMcqCount(Number(e.target.value))}
                >
                  {[2, 3, 4, 5, 6].map((n) => (
                    <option key={n} value={n}>
                      {n} questions
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="field-label" htmlFor="quiz-sa-count">
                  Short-Answer Questions (Copilot Graded)
                </label>
                <select
                  id="quiz-sa-count"
                  className="text-input"
                  value={shortCount}
                  onChange={(e) => setShortCount(Number(e.target.value))}
                >
                  {[0, 1, 2, 3].map((n) => (
                    <option key={n} value={n}>
                      {n === 0 ? '0 (MCQ only)' : `${n} open-ended`}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.6rem' }}>
              <button type="button" className="btn" onClick={onClose} disabled={Boolean(busyState)}>
                Skip for Now
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busyState === 'generating'}
                onClick={handleGenerate}
              >
                {busyState === 'generating'
                  ? 'Generating Quiz via Copilot SDK...'
                  : 'Generate Chapter Quiz'}
              </button>
            </div>
          </div>
        )}

        {/* Step B: Take Quiz */}
        {quiz && !isCompleted && (
          <form onSubmit={handleSubmit}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.35rem' }}>
              {quiz.questions.map((q, idx) => (
                <div
                  key={q.id}
                  style={{
                    padding: '1rem 1.1rem',
                    borderRadius: '12px',
                    border: '1px solid var(--border)',
                    background: 'var(--bg-app)',
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: '0.75rem', lineHeight: 1.45 }}>
                    <span style={{ color: 'var(--accent)', marginRight: '0.4rem' }}>
                      Q{idx + 1}.
                    </span>
                    {q.prompt}
                  </div>

                  {q.type === 'mcq' ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                      {q.options.map((opt, optIdx) => {
                        const selected = mcqSelections[q.id] === optIdx;
                        return (
                          <label
                            key={optIdx}
                            style={{
                              display: 'flex',
                              alignItems: 'flex-start',
                              gap: '0.6rem',
                              padding: '0.6rem 0.75rem',
                              borderRadius: '8px',
                              border: `1px solid ${selected ? 'var(--accent)' : 'var(--border)'}`,
                              background: selected ? 'var(--accent-soft)' : 'var(--bg-surface)',
                              cursor: 'pointer',
                            }}
                          >
                            <input
                              type="radio"
                              name={`quiz-${q.id}`}
                              checked={selected}
                              onChange={() =>
                                setMcqSelections((prev) => ({ ...prev, [q.id]: optIdx }))
                              }
                              style={{ marginTop: '0.22rem' }}
                            />
                            <span style={{ fontSize: '0.92rem', lineHeight: 1.45 }}>{opt}</span>
                          </label>
                        );
                      })}
                    </div>
                  ) : (
                    <div>
                      <textarea
                        className="text-input"
                        rows={4}
                        placeholder="Write your explanation in your own words..."
                        value={shortAnswers[q.id] ?? ''}
                        onChange={(e) =>
                          setShortAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))
                        }
                      />
                    </div>
                  )}
                </div>
              ))}
            </div>

            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginTop: '1.4rem',
              }}
            >
              <button
                type="button"
                className="btn"
                disabled={Boolean(busyState)}
                onClick={() => setQuiz(null)}
              >
                Regenerate Options
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={busyState === 'grading'}
              >
                {busyState === 'grading'
                  ? 'Evaluating Answers with Copilot SDK...'
                  : 'Submit & Grade Quiz'}
              </button>
            </div>
          </form>
        )}

        {/* Step C: Graded Results & Copilot Coaching */}
        {quiz && isCompleted && (
          <div>
            <div
              style={{
                padding: '1.1rem 1.2rem',
                borderRadius: '12px',
                background: 'var(--accent-soft)',
                border: '1px solid var(--border)',
                marginBottom: '1.25rem',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.4rem' }}>
                <span style={{ fontSize: '1.5rem', fontWeight: 800, color: 'var(--accent)' }}>
                  Score: {quiz.overallScorePct ?? 0}%
                </span>
                <span className="badge badge-success">Saved to Encrypted Vault</span>
              </div>
              {quiz.summaryTakeaway && (
                <p style={{ margin: 0, fontSize: '0.92rem', lineHeight: 1.5 }}>
                  {quiz.summaryTakeaway}
                </p>
              )}
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.1rem' }}>
              {quiz.questions.map((q, idx) => {
                const res = quiz.results[q.id];
                return (
                  <div
                    key={q.id}
                    style={{
                      padding: '1rem 1.1rem',
                      borderRadius: '12px',
                      border: '1px solid var(--border)',
                      background: 'var(--bg-app)',
                    }}
                  >
                    <div style={{ fontWeight: 600, marginBottom: '0.6rem' }}>
                      Q{idx + 1}. {q.prompt}
                    </div>

                    {q.type === 'mcq' ? (
                      <div style={{ fontSize: '0.9rem', lineHeight: 1.5 }}>
                        <div style={{ marginBottom: '0.4rem' }}>
                          <strong>Your Answer: </strong>
                          <span
                            style={{
                              color:
                                res?.type === 'mcq' && res.isCorrect
                                  ? 'var(--success)'
                                  : 'var(--danger)',
                              fontWeight: 600,
                            }}
                          >
                            {res?.type === 'mcq' && res.selectedIndex >= 0
                              ? q.options[res.selectedIndex]
                              : '(Unanswered)'}{' '}
                            {res?.type === 'mcq' && res.isCorrect ? '✓' : '✗'}
                          </span>
                        </div>
                        {!(res?.type === 'mcq' && res.isCorrect) && (
                          <div style={{ marginBottom: '0.4rem' }}>
                            <strong>Correct Answer: </strong>
                            <span style={{ color: 'var(--success)', fontWeight: 600 }}>
                              {q.options[q.correctIndex]}
                            </span>
                          </div>
                        )}
                        <div style={{ color: 'var(--text-secondary)' }}>
                          <strong>Explanation: </strong>
                          {q.explanation}
                        </div>
                      </div>
                    ) : (
                      <div style={{ fontSize: '0.9rem', lineHeight: 1.5 }}>
                        <div style={{ marginBottom: '0.45rem' }}>
                          <strong>Your Response: </strong>
                          <span>
                            {res?.type === 'short_answer' && res.answerText
                              ? res.answerText
                              : '(Unanswered)'}
                          </span>
                        </div>
                        {res?.type === 'short_answer' && (
                          <>
                            <div style={{ marginBottom: '0.45rem' }}>
                              <span
                                className={`badge ${
                                  res.verdict === 'strong' ? 'badge-success' : 'badge-warn'
                                }`}
                              >
                                {res.scorePct}% ({res.verdict})
                              </span>
                            </div>
                            <div style={{ marginBottom: '0.45rem' }}>
                              <strong>Copilot Feedback: </strong>
                              {res.feedback}
                            </div>
                            {res.missedPoints.length > 0 && (
                              <div style={{ marginBottom: '0.45rem', color: 'var(--text-secondary)' }}>
                                <strong>Key Points to Review: </strong>
                                {res.missedPoints.join(' · ')}
                              </div>
                            )}
                          </>
                        )}
                        <div
                          style={{
                            marginTop: '0.5rem',
                            padding: '0.6rem 0.75rem',
                            borderRadius: '8px',
                            background: 'var(--bg-surface)',
                            border: '1px solid var(--border)',
                            color: 'var(--text-secondary)',
                          }}
                        >
                          <strong>Reference Answer: </strong>
                          {q.sampleAnswer}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                marginTop: '1.4rem',
                gap: '0.75rem',
              }}
            >
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setQuiz(null);
                  setMcqSelections({});
                  setShortAnswers({});
                }}
              >
                Generate Fresh Quiz
              </button>
              <div style={{ display: 'flex', gap: '0.6rem' }}>
                <button type="button" className="btn" onClick={onClose}>
                  Back to Chapter
                </button>
                {hasNextChapter && (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => {
                      onClose();
                      onNextChapter();
                    }}
                  >
                    Next Chapter →
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
