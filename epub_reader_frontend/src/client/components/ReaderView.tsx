import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { readerApi } from '../api.js';
import {
  applyHighlightsToDom,
  captureSelectionAnchor,
  type TextSelectorTarget,
} from '../highlighter.js';
import type {
  Annotation,
  BookDetailResponse,
  Bookmark,
  ChapterContentResponse,
  ChapterQuizRecord,
  CopilotStatusResponse,
  HighlightColor,
  ReaderTheme,
  TocItem,
} from '../../shared/types.js';
import { QuizModal } from './QuizModal.js';

interface ReaderViewProps {
  bookId: string;
  copilotStatus: CopilotStatusResponse | null;
  theme: ReaderTheme;
  onChangeTheme: (theme: ReaderTheme) => void;
  onBackToLibrary: () => void;
  onLockVault: () => Promise<void>;
}

const HIGHLIGHT_SWATCHES: Array<{ color: HighlightColor; bg: string; label: string }> = [
  { color: 'amber', bg: '#fbbf24', label: 'Amber' },
  { color: 'emerald', bg: '#34d399', label: 'Emerald' },
  { color: 'sky', bg: '#38bdf8', label: 'Sky' },
  { color: 'rose', bg: '#fb7185', label: 'Rose' },
  { color: 'violet', bg: '#a78bfa', label: 'Violet' },
];

function TocTree({
  items,
  activeSpineIndex,
  onSelect,
  depth = 0,
}: {
  items: TocItem[];
  activeSpineIndex: number;
  onSelect: (spineIndex: number, fragment?: string) => void;
  depth?: number;
}) {
  return (
    <ul style={{ listStyle: 'none', margin: 0, paddingLeft: depth === 0 ? 0 : '0.9rem' }}>
      {items.map((item) => {
        const active = item.spineIndex === activeSpineIndex && !item.fragment;
        return (
          <li key={item.id} style={{ marginBottom: '0.2rem' }}>
            <button
              type="button"
              onClick={() => onSelect(item.spineIndex, item.fragment)}
              style={{
                width: '100%',
                textAlign: 'left',
                border: 'none',
                background: active ? 'var(--accent-soft)' : 'transparent',
                color: active ? 'var(--accent)' : 'var(--text-primary)',
                fontWeight: active ? 600 : 400,
                padding: '0.42rem 0.55rem',
                borderRadius: '6px',
                cursor: 'pointer',
                fontSize: '0.87rem',
                lineHeight: 1.4,
              }}
            >
              {item.label}
            </button>
            {item.children.length > 0 && (
              <TocTree
                items={item.children}
                activeSpineIndex={activeSpineIndex}
                onSelect={onSelect}
                depth={depth + 1}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function ReaderView({
  bookId,
  copilotStatus,
  theme,
  onChangeTheme,
  onBackToLibrary,
  onLockVault,
}: ReaderViewProps) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const chapterRootRef = useRef<HTMLDivElement | null>(null);
  const pendingScrollTargetRef = useRef<
    | { kind: 'ratio'; ratio: number }
    | { kind: 'fragment'; fragment: string }
    | { kind: 'annotation'; annotationId: string }
    | null
  >(null);

  const [detail, setDetail] = useState<BookDetailResponse | null>(null);
  const [chapter, setChapter] = useState<ChapterContentResponse | null>(null);
  const [loadingChapter, setLoadingChapter] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Drawers & Typography
  const [showToc, setShowToc] = useState(false);
  const [showStudyDrawer, setShowStudyDrawer] = useState(false);
  const [drawerTab, setDrawerTab] = useState<'notes' | 'bookmarks' | 'quizzes'>('notes');
  const [showTypography, setShowTypography] = useState(false);
  const [fontSize, setFontSize] = useState(18);
  const [lineHeight, setLineHeight] = useState(1.75);
  const [maxWidthPx, setMaxWidthPx] = useState(740);
  const [fontFamilyMode, setFontFamilyMode] = useState<'serif' | 'sans'>('serif');

  // Selection / Annotation Composer
  const [pendingSelection, setPendingSelection] = useState<{
    anchor: TextSelectorTarget;
    x: number;
    y: number;
  } | null>(null);
  const [composerColor, setComposerColor] = useState<HighlightColor>('amber');
  const [composerNote, setComposerNote] = useState('');

  // Active clicked annotation editor
  const [editingAnnotation, setEditingAnnotation] = useState<Annotation | null>(null);
  const [editingNoteDraft, setEditingNoteDraft] = useState('');
  const [editingColorDraft, setEditingColorDraft] = useState<HighlightColor>('amber');

  // Quiz modal
  const [quizModalQuiz, setQuizModalQuiz] = useState<ChapterQuizRecord | null | undefined>(
    undefined,
  ); // undefined = closed, null = new quiz setup, ChapterQuizRecord = view/continue quiz

  const activeSpineIndex = chapter?.spineIndex ?? detail?.state.progress.spineIndex ?? 0;

  const loadChapter = useCallback(
    async (
      spineIndex: number,
      scrollTarget?:
        | { kind: 'ratio'; ratio: number }
        | { kind: 'fragment'; fragment: string }
        | { kind: 'annotation'; annotationId: string },
    ) => {
      setLoadingChapter(true);
      setError(null);
      setPendingSelection(null);
      setEditingAnnotation(null);
      pendingScrollTargetRef.current = scrollTarget ?? { kind: 'ratio', ratio: 0 };
      try {
        const ch = await readerApi.getChapter(bookId, spineIndex);
        setChapter(ch);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load chapter');
      } finally {
        setLoadingChapter(false);
      }
    },
    [bookId],
  );

  // Initial book detail & saved chapter load
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await readerApi.getBookDetail(bookId);
        if (cancelled) return;
        setDetail(loaded);
        const startSpine = Math.min(
          Math.max(0, loaded.state.progress.spineIndex),
          Math.max(0, loaded.spine.length - 1),
        );
        await loadChapter(startSpine, {
          kind: 'ratio',
          ratio: loaded.state.progress.scrollRatio,
        });
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to open encrypted book');
          setLoadingChapter(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bookId, loadChapter]);

  const chapterAnnotations = useMemo(() => {
    if (!detail || !chapter) return [];
    return detail.state.annotations.filter((a) => a.spineIndex === chapter.spineIndex);
  }, [detail, chapter]);

  const chapterQuizzes = useMemo(() => {
    if (!detail || !chapter) return [];
    return detail.state.quizzes.filter((q) => q.spineIndex === chapter.spineIndex);
  }, [detail, chapter]);

  const bestChapterQuiz = useMemo(() => {
    const completed = chapterQuizzes.filter((q) => q.completedAt);
    if (completed.length === 0) return null;
    return completed.reduce((best, cur) =>
      (cur.overallScorePct ?? 0) > (best.overallScorePct ?? 0) ? cur : best,
    );
  }, [chapterQuizzes]);

  // Render sanitized HTML + apply DOM highlights cleanly
  useEffect(() => {
    const root = chapterRootRef.current;
    if (!root || !chapter) return;

    root.innerHTML = chapter.html;
    applyHighlightsToDom(
      root,
      chapterAnnotations.map((a) => ({
        id: a.id,
        startOffset: a.startOffset,
        endOffset: a.endOffset,
        selectedText: a.selectedText,
        prefix: a.prefix,
        suffix: a.suffix,
        color: a.color,
        hasNote: Boolean(a.noteMarkdown.trim()),
      })),
    );

    const target = pendingScrollTargetRef.current;
    pendingScrollTargetRef.current = null;
    const viewport = viewportRef.current;
    if (!target || !viewport) return;

    requestAnimationFrame(() => {
      if (target.kind === 'ratio') {
        const maxScroll = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
        viewport.scrollTop = maxScroll * target.ratio;
      } else if (target.kind === 'fragment' && target.fragment) {
        const el = root.querySelector(`[id="${CSS.escape(target.fragment)}"]`);
        el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else if (target.kind === 'annotation') {
        const mark = root.querySelector(
          `mark[data-annotation-id="${CSS.escape(target.annotationId)}"]`,
        );
        if (mark) {
          mark.classList.add('epub-hl-flash');
          mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
          setTimeout(() => mark.classList.remove('epub-hl-flash'), 2600);
        }
      }
    });
  }, [chapter, chapterAnnotations]);

  // Debounced scroll progress saving
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !chapter) return;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const onScroll = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(async () => {
        const maxScroll = Math.max(1, viewport.scrollHeight - viewport.clientHeight);
        const ratio = Math.min(1, Math.max(0, viewport.scrollTop / maxScroll));
        try {
          const progress = await readerApi.saveProgress(bookId, {
            spineIndex: chapter.spineIndex,
            scrollRatio: Number(ratio.toFixed(4)),
          });
          setDetail((prev) =>
            prev ? { ...prev, state: { ...prev.state, progress } } : prev,
          );
        } catch {
          // ignore transient progress save errors
        }
      }, 650);
    };

    viewport.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      if (timer) clearTimeout(timer);
      viewport.removeEventListener('scroll', onScroll);
    };
  }, [bookId, chapter]);

  // Keyboard chapter navigation (ArrowLeft / ArrowRight when not typing)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (quizModalQuiz !== undefined) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (!detail || !chapter) return;
      if (e.key === 'ArrowLeft' && chapter.spineIndex > 0) {
        void loadChapter(chapter.spineIndex - 1);
      } else if (e.key === 'ArrowRight' && chapter.spineIndex + 1 < detail.spine.length) {
        void loadChapter(chapter.spineIndex + 1);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [chapter, detail, loadChapter, quizModalQuiz]);

  // Handle clicks inside chapter HTML (internal chapter links + existing highlight marks)
  const handleChapterClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement | null;
    if (!target) return;

    const markEl = target.closest('mark[data-annotation-id]') as HTMLElement | null;
    if (markEl && detail) {
      const annId = markEl.dataset.annotationId;
      const found = detail.state.annotations.find((a) => a.id === annId);
      if (found) {
        setPendingSelection(null);
        setEditingAnnotation(found);
        setEditingColorDraft(found.color);
        setEditingNoteDraft(found.noteMarkdown);
        return;
      }
    }

    const linkEl = target.closest('a[data-epub-spine]') as HTMLAnchorElement | null;
    if (linkEl) {
      e.preventDefault();
      const targetSpine = Number(linkEl.dataset.epubSpine);
      const fragment = linkEl.dataset.epubFragment;
      if (!Number.isNaN(targetSpine)) {
        if (chapter && targetSpine === chapter.spineIndex && fragment) {
          const fragEl = chapterRootRef.current?.querySelector(
            `[id="${CSS.escape(fragment)}"]`,
          );
          fragEl?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
          void loadChapter(
            targetSpine,
            fragment ? { kind: 'fragment', fragment } : { kind: 'ratio', ratio: 0 },
          );
        }
      }
    }
  };

  // Capture text selection for Highlight + Note
  const handleChapterMouseUp = () => {
    const root = chapterRootRef.current;
    const sel = window.getSelection();
    if (!root || !sel || sel.isCollapsed) {
      return;
    }
    const anchor = captureSelectionAnchor(root, sel);
    if (!anchor) return;
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const x = Math.min(window.innerWidth - 330, Math.max(16, rect.left));
    const y = Math.max(64, rect.top - 12);
    setComposerColor('amber');
    setComposerNote('');
    setEditingAnnotation(null);
    setPendingSelection({ anchor, x, y });
  };

  const handleCreateAnnotation = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!pendingSelection || !chapter) return;
    try {
      const created = await readerApi.createAnnotation(bookId, {
        spineIndex: chapter.spineIndex,
        chapterTitle: chapter.title,
        ...pendingSelection.anchor,
        color: composerColor,
        noteMarkdown: composerNote,
      });
      window.getSelection()?.removeAllRanges();
      setPendingSelection(null);
      setComposerNote('');
      setDetail((prev) =>
        prev
          ? {
              ...prev,
              state: {
                ...prev.state,
                annotations: [...prev.state.annotations, created],
              },
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save highlight');
    }
  };

  const handleSaveEditedAnnotation = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingAnnotation) return;
    try {
      const updated = await readerApi.updateAnnotation(bookId, editingAnnotation.id, {
        color: editingColorDraft,
        noteMarkdown: editingNoteDraft,
      });
      setEditingAnnotation(null);
      setDetail((prev) =>
        prev
          ? {
              ...prev,
              state: {
                ...prev.state,
                annotations: prev.state.annotations.map((a) =>
                  a.id === updated.id ? updated : a,
                ),
              },
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update note');
    }
  };

  const handleDeleteAnnotation = async (annotationId: string) => {
    try {
      await readerApi.deleteAnnotation(bookId, annotationId);
      setEditingAnnotation(null);
      setDetail((prev) =>
        prev
          ? {
              ...prev,
              state: {
                ...prev.state,
                annotations: prev.state.annotations.filter((a) => a.id !== annotationId),
              },
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete highlight');
    }
  };

  const handleAddBookmark = async () => {
    const viewport = viewportRef.current;
    if (!viewport || !chapter) return;
    const maxScroll = Math.max(1, viewport.scrollHeight - viewport.clientHeight);
    const scrollRatio = Math.min(1, Math.max(0, viewport.scrollTop / maxScroll));
    const fullText = (chapterRootRef.current?.textContent ?? '').replace(/\s+/g, ' ').trim();
    const approxCharIdx = Math.floor(fullText.length * scrollRatio);
    const snippet = fullText.slice(approxCharIdx, approxCharIdx + 120).trim();
    const defaultLabel = `${chapter.title} (${Math.round(scrollRatio * 100)}%)`;

    try {
      const created = await readerApi.createBookmark(bookId, {
        spineIndex: chapter.spineIndex,
        chapterTitle: chapter.title,
        scrollRatio: Number(scrollRatio.toFixed(4)),
        snippet,
        label: defaultLabel,
      });
      setDetail((prev) =>
        prev
          ? {
              ...prev,
              state: {
                ...prev.state,
                bookmarks: [created, ...prev.state.bookmarks],
              },
            }
          : prev,
      );
      setDrawerTab('bookmarks');
      setShowStudyDrawer(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add bookmark');
    }
  };

  const handleDeleteBookmark = async (bookmarkId: string) => {
    try {
      await readerApi.deleteBookmark(bookId, bookmarkId);
      setDetail((prev) =>
        prev
          ? {
              ...prev,
              state: {
                ...prev.state,
                bookmarks: prev.state.bookmarks.filter((b) => b.id !== bookmarkId),
              },
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete bookmark');
    }
  };

  const totalChapters = detail?.spine.length ?? 1;
  const hasPrev = activeSpineIndex > 0;
  const hasNext = activeSpineIndex + 1 < totalChapters;

  return (
    <div className="reader-shell">
      {/* Scoped Book Stylesheets */}
      {chapter && chapter.styles.length > 0 && (
        <style dangerouslySetInnerHTML={{ __html: chapter.styles.join('\n') }} />
      )}

      {/* Top Reader Toolbar */}
      <header className="reader-topbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', minWidth: 0 }}>
          <button type="button" className="btn" onClick={onBackToLibrary}>
            ← Library
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setShowToc((v) => !v)}
          >
            TOC
          </button>
          <div
            style={{
              marginLeft: '0.35rem',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              fontSize: '0.9rem',
              fontWeight: 600,
            }}
            title={chapter?.title ?? detail?.book.title}
          >
            {chapter?.title ?? detail?.book.title ?? 'Loading...'}
            <span style={{ color: 'var(--text-muted)', fontWeight: 400, marginLeft: '0.5rem' }}>
              ({activeSpineIndex + 1}/{totalChapters})
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', position: 'relative' }}>
          <button
            type="button"
            className="btn"
            onClick={handleAddBookmark}
            title="Bookmark current reading position"
          >
            + Bookmark
          </button>

          <button
            type="button"
            className="btn"
            onClick={() => setShowStudyDrawer((v) => !v)}
          >
            Notes & Bookmarks (
            {(detail?.state.annotations.length ?? 0) + (detail?.state.bookmarks.length ?? 0)})
          </button>

          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              const unsubmitted = chapterQuizzes.find((q) => !q.completedAt);
              setQuizModalQuiz(unsubmitted ?? null);
            }}
          >
            Chapter Quiz
          </button>

          <button
            type="button"
            className="btn"
            onClick={() => setShowTypography((v) => !v)}
            title="Typography & Theme"
          >
            Aa
          </button>

          <button type="button" className="btn btn-danger" onClick={onLockVault} title="Lock Vault">
            Lock
          </button>

          {showTypography && (
            <div
              style={{
                position: 'absolute',
                top: '48px',
                right: 0,
                width: '280px',
                background: 'var(--bg-surface)',
                border: '1px solid var(--border)',
                borderRadius: '12px',
                padding: '1rem',
                boxShadow: 'var(--shadow)',
                zIndex: 40,
              }}
            >
              <div style={{ marginBottom: '0.8rem' }}>
                <span className="field-label">Theme</span>
                <div style={{ display: 'flex', gap: '0.4rem' }}>
                  {(['light', 'sepia', 'dark'] as ReaderTheme[]).map((t) => (
                    <button
                      key={t}
                      type="button"
                      className={`btn ${theme === t ? 'btn-primary' : ''}`}
                      style={{ flex: 1, textTransform: 'capitalize' }}
                      onClick={() => onChangeTheme(t)}
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>

              <div style={{ marginBottom: '0.8rem' }}>
                <span className="field-label">Font Style</span>
                <div style={{ display: 'flex', gap: '0.4rem' }}>
                  <button
                    type="button"
                    className={`btn ${fontFamilyMode === 'serif' ? 'btn-primary' : ''}`}
                    style={{ flex: 1 }}
                    onClick={() => setFontFamilyMode('serif')}
                  >
                    Serif
                  </button>
                  <button
                    type="button"
                    className={`btn ${fontFamilyMode === 'sans' ? 'btn-primary' : ''}`}
                    style={{ flex: 1 }}
                    onClick={() => setFontFamilyMode('sans')}
                  >
                    Sans
                  </button>
                </div>
              </div>

              <div style={{ marginBottom: '0.8rem' }}>
                <span className="field-label">Font Size ({fontSize}px)</span>
                <div style={{ display: 'flex', gap: '0.4rem' }}>
                  <button
                    type="button"
                    className="btn"
                    style={{ flex: 1 }}
                    onClick={() => setFontSize((s) => Math.max(14, s - 1))}
                  >
                    A-
                  </button>
                  <button
                    type="button"
                    className="btn"
                    style={{ flex: 1 }}
                    onClick={() => setFontSize((s) => Math.min(28, s + 1))}
                  >
                    A+
                  </button>
                </div>
              </div>

              <div style={{ marginBottom: '0.8rem' }}>
                <span className="field-label">Line Height ({lineHeight.toFixed(2)})</span>
                <input
                  type="range"
                  min={1.4}
                  max={2.2}
                  step={0.05}
                  value={lineHeight}
                  onChange={(e) => setLineHeight(Number(e.target.value))}
                  style={{ width: '100%' }}
                />
              </div>

              <div>
                <span className="field-label">Page Width ({maxWidthPx}px)</span>
                <input
                  type="range"
                  min={560}
                  max={1040}
                  step={40}
                  value={maxWidthPx}
                  onChange={(e) => setMaxWidthPx(Number(e.target.value))}
                  style={{ width: '100%' }}
                />
              </div>
            </div>
          )}
        </div>
      </header>

      <div className="reader-body">
        {/* Left TOC Drawer */}
        {showToc && detail && (
          <aside className="reader-drawer">
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: '0.75rem',
              }}
            >
              <h3 style={{ margin: 0, fontSize: '0.98rem' }}>Table of Contents</h3>
              <button type="button" className="btn" onClick={() => setShowToc(false)}>
                ✕
              </button>
            </div>
            <TocTree
              items={detail.toc}
              activeSpineIndex={activeSpineIndex}
              onSelect={(spineIdx, fragment) => {
                void loadChapter(
                  spineIdx,
                  fragment ? { kind: 'fragment', fragment } : { kind: 'ratio', ratio: 0 },
                );
              }}
            />
          </aside>
        )}

        {/* Main Chapter Reading Surface */}
        <main ref={viewportRef} className="reader-viewport">
          {error && (
            <div
              role="alert"
              style={{
                maxWidth: `${maxWidthPx}px`,
                margin: '0 auto 1.25rem',
                padding: '0.8rem 1rem',
                borderRadius: '10px',
                background: 'rgba(220, 38, 38, 0.1)',
                color: 'var(--danger)',
              }}
            >
              {error}
            </div>
          )}

          {loadingChapter ? (
            <div style={{ textAlign: 'center', padding: '4rem 1rem', color: 'var(--text-muted)' }}>
              Decrypting chapter in memory...
            </div>
          ) : (
            chapter && (
              <div
                className="epub-chapter-container"
                style={{
                  maxWidth: `${maxWidthPx}px`,
                  fontSize: `${fontSize}px`,
                  lineHeight,
                  fontFamily:
                    fontFamilyMode === 'serif'
                      ? "'Noto Serif SC', 'Source Han Serif SC', Georgia, 'Times New Roman', serif"
                      : "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
                }}
              >
                <div
                  ref={chapterRootRef}
                  className="epub-chapter-scope"
                  onClick={handleChapterClick}
                  onMouseUp={handleChapterMouseUp}
                />

                {/* Post-Chapter Copilot SDK Comprehension Check Card */}
                <section className="chapter-end-card">
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      flexWrap: 'wrap',
                      gap: '0.75rem',
                      marginBottom: '0.6rem',
                    }}
                  >
                    <span className="badge badge-success">End of Chapter Active Recall</span>
                    {bestChapterQuiz && (
                      <span className="badge badge-success">
                        Best Score: {bestChapterQuiz.overallScorePct ?? 0}%
                      </span>
                    )}
                  </div>
                  <h3 style={{ margin: '0 0 0.4rem', fontSize: '1.15rem' }}>
                    Ready to test what you learned in “{chapter.title}”?
                  </h3>
                  <p
                    style={{
                      margin: '0 0 1.15rem',
                      fontSize: '0.92rem',
                      color: 'var(--text-secondary)',
                    }}
                  >
                    Generate a personalized multiple-choice &amp; short-answer quiz with GitHub
                    Copilot SDK before moving to the next chapter.
                  </p>

                  <div
                    style={{
                      display: 'flex',
                      flexWrap: 'wrap',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: '0.75rem',
                    }}
                  >
                    <div style={{ display: 'flex', gap: '0.55rem', flexWrap: 'wrap' }}>
                      <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => setQuizModalQuiz(null)}
                      >
                        {bestChapterQuiz ? 'Take New Chapter Quiz' : 'Take Chapter Quiz'}
                      </button>
                      {bestChapterQuiz && (
                        <button
                          type="button"
                          className="btn"
                          onClick={() => setQuizModalQuiz(bestChapterQuiz)}
                        >
                          Review Last Quiz ({bestChapterQuiz.overallScorePct}%)
                        </button>
                      )}
                    </div>

                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                      <button
                        type="button"
                        className="btn"
                        disabled={!hasPrev}
                        onClick={() => hasPrev && void loadChapter(activeSpineIndex - 1)}
                      >
                        ← Prev Chapter
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={!hasNext}
                        onClick={() => hasNext && void loadChapter(activeSpineIndex + 1)}
                      >
                        Next Chapter →
                      </button>
                    </div>
                  </div>
                </section>
              </div>
            )
          )}
        </main>

        {/* Right Notes, Bookmarks, & Quizzes Drawer */}
        {showStudyDrawer && detail && (
          <aside className="reader-drawer reader-drawer-right">
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: '0.75rem',
              }}
            >
              <h3 style={{ margin: 0, fontSize: '0.98rem' }}>Study Notebook</h3>
              <div style={{ display: 'flex', gap: '0.35rem' }}>
                <button
                  type="button"
                  className="btn"
                  style={{ padding: '0.3rem 0.55rem', fontSize: '0.78rem' }}
                  onClick={() => void readerApi.downloadExportMarkdown(bookId)}
                >
                  Export .md
                </button>
                <button type="button" className="btn" onClick={() => setShowStudyDrawer(false)}>
                  ✕
                </button>
              </div>
            </div>

            <div style={{ display: 'flex', gap: '0.35rem', marginBottom: '0.9rem' }}>
              <button
                type="button"
                className={`btn ${drawerTab === 'notes' ? 'btn-primary' : ''}`}
                style={{ flex: 1, padding: '0.4rem' }}
                onClick={() => setDrawerTab('notes')}
              >
                Notes ({detail.state.annotations.length})
              </button>
              <button
                type="button"
                className={`btn ${drawerTab === 'bookmarks' ? 'btn-primary' : ''}`}
                style={{ flex: 1, padding: '0.4rem' }}
                onClick={() => setDrawerTab('bookmarks')}
              >
                Bookmarks ({detail.state.bookmarks.length})
              </button>
              <button
                type="button"
                className={`btn ${drawerTab === 'quizzes' ? 'btn-primary' : ''}`}
                style={{ flex: 1, padding: '0.4rem' }}
                onClick={() => setDrawerTab('quizzes')}
              >
                Quizzes ({detail.state.quizzes.length})
              </button>
            </div>

            {drawerTab === 'notes' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                {detail.state.annotations.length === 0 ? (
                  <p style={{ color: 'var(--text-muted)', fontSize: '0.86rem' }}>
                    Select any text inside a chapter to highlight it and attach a study note.
                  </p>
                ) : (
                  detail.state.annotations.map((ann) => (
                    <div
                      key={ann.id}
                      style={{
                        padding: '0.75rem',
                        borderRadius: '10px',
                        border: '1px solid var(--border)',
                        background: 'var(--bg-app)',
                        fontSize: '0.85rem',
                      }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          color: 'var(--text-muted)',
                          fontSize: '0.75rem',
                          marginBottom: '0.35rem',
                        }}
                      >
                        <span>{ann.chapterTitle}</span>
                        <button
                          type="button"
                          onClick={() => void handleDeleteAnnotation(ann.id)}
                          style={{
                            border: 'none',
                            background: 'transparent',
                            color: 'var(--danger)',
                            cursor: 'pointer',
                          }}
                        >
                          Delete
                        </button>
                      </div>
                      <blockquote
                        onClick={() =>
                          void loadChapter(ann.spineIndex, {
                            kind: 'annotation',
                            annotationId: ann.id,
                          })
                        }
                        style={{
                          margin: '0 0 0.45rem',
                          paddingLeft: '0.6rem',
                          borderLeft: '3px solid var(--accent)',
                          cursor: 'pointer',
                          color: 'var(--text-secondary)',
                        }}
                      >
                        “{ann.selectedText.slice(0, 180)}
                        {ann.selectedText.length > 180 ? '…' : ''}”
                      </blockquote>
                      {ann.noteMarkdown && (
                        <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.45 }}>
                          {ann.noteMarkdown}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            )}

            {drawerTab === 'bookmarks' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>
                {detail.state.bookmarks.length === 0 ? (
                  <p style={{ color: 'var(--text-muted)', fontSize: '0.86rem' }}>
                    Click “+ Bookmark” in the top bar to save your reading spot.
                  </p>
                ) : (
                  detail.state.bookmarks.map((bm: Bookmark) => (
                    <div
                      key={bm.id}
                      style={{
                        padding: '0.7rem',
                        borderRadius: '10px',
                        border: '1px solid var(--border)',
                        background: 'var(--bg-app)',
                        fontSize: '0.85rem',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.5rem' }}>
                        <button
                          type="button"
                          onClick={() =>
                            void loadChapter(bm.spineIndex, {
                              kind: 'ratio',
                              ratio: bm.scrollRatio,
                            })
                          }
                          style={{
                            border: 'none',
                            background: 'transparent',
                            padding: 0,
                            textAlign: 'left',
                            color: 'var(--accent)',
                            fontWeight: 600,
                            cursor: 'pointer',
                          }}
                        >
                          {bm.label}
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleDeleteBookmark(bm.id)}
                          style={{
                            border: 'none',
                            background: 'transparent',
                            color: 'var(--danger)',
                            cursor: 'pointer',
                            fontSize: '0.78rem',
                          }}
                        >
                          ✕
                        </button>
                      </div>
                      {bm.snippet && (
                        <div
                          style={{
                            marginTop: '0.35rem',
                            color: 'var(--text-secondary)',
                            fontSize: '0.8rem',
                          }}
                        >
                          “{bm.snippet.slice(0, 100)}…”
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            )}

            {drawerTab === 'quizzes' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem' }}>
                {detail.state.quizzes.length === 0 ? (
                  <p style={{ color: 'var(--text-muted)', fontSize: '0.86rem' }}>
                    No chapter quizzes taken yet. Click “Chapter Quiz” when you finish a chapter!
                  </p>
                ) : (
                  detail.state.quizzes.map((qz) => (
                    <div
                      key={qz.id}
                      onClick={() => setQuizModalQuiz(qz)}
                      style={{
                        padding: '0.75rem',
                        borderRadius: '10px',
                        border: '1px solid var(--border)',
                        background: 'var(--bg-app)',
                        cursor: 'pointer',
                        fontSize: '0.85rem',
                      }}
                    >
                      <div style={{ fontWeight: 600, marginBottom: '0.25rem' }}>
                        {qz.chapterTitle}
                      </div>
                      <div style={{ display: 'flex', gap: '0.45rem', alignItems: 'center' }}>
                        <span className={`badge ${qz.completedAt ? 'badge-success' : 'badge-warn'}`}>
                          {qz.completedAt ? `Score: ${qz.overallScorePct ?? 0}%` : 'In Progress'}
                        </span>
                        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                          {qz.questions.length} questions
                        </span>
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
          </aside>
        )}
      </div>

      {/* Floating Selection Highlight & Note Composer */}
      {pendingSelection && (
        <div
          className="selection-popover"
          style={{ left: `${pendingSelection.x}px`, top: `${pendingSelection.y}px` }}
        >
          <form onSubmit={handleCreateAnnotation}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: '0.55rem',
              }}
            >
              <div style={{ display: 'flex', gap: '0.4rem' }}>
                {HIGHLIGHT_SWATCHES.map((sw) => (
                  <button
                    key={sw.color}
                    type="button"
                    aria-label={sw.label}
                    className={`swatch-btn ${composerColor === sw.color ? 'active' : ''}`}
                    style={{ background: sw.bg }}
                    onClick={() => setComposerColor(sw.color)}
                  />
                ))}
              </div>
              <button
                type="button"
                className="btn"
                style={{ padding: '0.15rem 0.45rem', fontSize: '0.75rem' }}
                onClick={() => setPendingSelection(null)}
              >
                ✕
              </button>
            </div>
            <textarea
              className="text-input"
              rows={3}
              placeholder="Optional study note (Markdown supported)..."
              value={composerNote}
              onChange={(e) => setComposerNote(e.target.value)}
              style={{ fontSize: '0.84rem', marginBottom: '0.5rem' }}
            />
            <button type="submit" className="btn btn-primary" style={{ width: '100%' }}>
              Save Highlight{composerNote.trim() ? ' & Note' : ''}
            </button>
          </form>
        </div>
      )}

      {/* Existing Highlight Edit Dialog */}
      {editingAnnotation && (
        <div className="modal-backdrop">
          <div className="modal-panel" style={{ maxWidth: '480px' }}>
            <h3 style={{ margin: '0 0 0.6rem' }}>Edit Highlight &amp; Study Note</h3>
            <blockquote
              style={{
                margin: '0 0 0.9rem',
                paddingLeft: '0.75rem',
                borderLeft: '3px solid var(--accent)',
                color: 'var(--text-secondary)',
                fontSize: '0.9rem',
              }}
            >
              “{editingAnnotation.selectedText.slice(0, 220)}”
            </blockquote>
            <form onSubmit={handleSaveEditedAnnotation}>
              <div style={{ display: 'flex', gap: '0.45rem', marginBottom: '0.8rem' }}>
                {HIGHLIGHT_SWATCHES.map((sw) => (
                  <button
                    key={sw.color}
                    type="button"
                    className={`swatch-btn ${editingColorDraft === sw.color ? 'active' : ''}`}
                    style={{ background: sw.bg }}
                    onClick={() => setEditingColorDraft(sw.color)}
                  />
                ))}
              </div>
              <textarea
                className="text-input"
                rows={4}
                placeholder="Write your note..."
                value={editingNoteDraft}
                onChange={(e) => setEditingNoteDraft(e.target.value)}
              />
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  marginTop: '1rem',
                }}
              >
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => void handleDeleteAnnotation(editingAnnotation.id)}
                >
                  Delete Highlight
                </button>
                <div style={{ display: 'flex', gap: '0.5rem' }}>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setEditingAnnotation(null)}
                  >
                    Cancel
                  </button>
                  <button type="submit" className="btn btn-primary">
                    Save Changes
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Copilot SDK Chapter Quiz Modal */}
      {quizModalQuiz !== undefined && chapter && (
        <QuizModal
          bookId={bookId}
          spineIndex={quizModalQuiz?.spineIndex ?? chapter.spineIndex}
          chapterTitle={quizModalQuiz?.chapterTitle ?? chapter.title}
          initialQuiz={quizModalQuiz}
          copilotStatus={copilotStatus}
          hasNextChapter={hasNext}
          onQuizSaved={(savedQuiz, updatedProgress) => {
            setDetail((prev) => {
              if (!prev) return prev;
              const exists = prev.state.quizzes.some((q) => q.id === savedQuiz.id);
              const nextQuizzes = exists
                ? prev.state.quizzes.map((q) => (q.id === savedQuiz.id ? savedQuiz : q))
                : [savedQuiz, ...prev.state.quizzes];
              return {
                ...prev,
                state: {
                  ...prev.state,
                  progress: updatedProgress ?? prev.state.progress,
                  quizzes: nextQuizzes,
                },
              };
            });
          }}
          onNextChapter={() => {
            if (hasNext) void loadChapter(activeSpineIndex + 1);
          }}
          onClose={() => setQuizModalQuiz(undefined)}
        />
      )}
    </div>
  );
}
