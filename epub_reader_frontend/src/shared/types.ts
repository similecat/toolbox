export type HighlightColor = 'amber' | 'emerald' | 'sky' | 'rose' | 'violet';

export type ReaderTheme = 'light' | 'sepia' | 'dark';

export interface VaultStatusResponse {
  initialized: boolean;
  unlocked: boolean;
  dpapiSupported: boolean;
  dpapiRememberEnabled: boolean;
  csrfToken?: string;
}

export interface ReadingProgress {
  spineIndex: number;
  scrollRatio: number;
  completedChapters: number[];
  updatedAt: string;
}

export interface BookSummary {
  bookId: string;
  title: string;
  author: string;
  language: string;
  description?: string;
  chapterCount: number;
  epubByteLength: number;
  coverDataUrl?: string;
  progress: ReadingProgress;
  importedAt: string;
  lastReadAt?: string;
}

export interface TocItem {
  id: string;
  label: string;
  href: string;
  spineIndex: number;
  fragment?: string;
  children: TocItem[];
}

export interface SpineChapterSummary {
  spineIndex: number;
  id: string;
  href: string;
  title: string;
  charCount: number;
}

export interface Bookmark {
  id: string;
  spineIndex: number;
  chapterTitle: string;
  scrollRatio: number;
  snippet: string;
  label: string;
  createdAt: string;
}

export interface Annotation {
  id: string;
  spineIndex: number;
  chapterTitle: string;
  startOffset: number;
  endOffset: number;
  selectedText: string;
  prefix: string;
  suffix: string;
  color: HighlightColor;
  noteMarkdown: string;
  createdAt: string;
  updatedAt: string;
}

export interface McqQuestion {
  id: string;
  type: 'mcq';
  prompt: string;
  options: [string, string, string, string];
  correctIndex: 0 | 1 | 2 | 3;
  explanation: string;
  sourceExcerpt?: string;
}

export interface ShortAnswerQuestion {
  id: string;
  type: 'short_answer';
  prompt: string;
  rubricKeyPoints: string[];
  sampleAnswer: string;
}

export type QuizQuestion = McqQuestion | ShortAnswerQuestion;

export interface McqUserAnswer {
  type: 'mcq';
  selectedIndex: number;
  isCorrect: boolean;
  scorePct: number;
}

export interface ShortAnswerEvaluation {
  type: 'short_answer';
  answerText: string;
  scorePct: number;
  verdict: 'strong' | 'partial' | 'needs_review';
  feedback: string;
  missedPoints: string[];
}

export type UserQuestionResult = McqUserAnswer | ShortAnswerEvaluation;

export interface ChapterQuizRecord {
  id: string;
  spineIndex: number;
  chapterTitle: string;
  language: string;
  createdAt: string;
  completedAt?: string;
  overallScorePct?: number;
  summaryTakeaway?: string;
  questions: QuizQuestion[];
  results: Record<string, UserQuestionResult>;
}

export interface BookStudyState {
  version: 1;
  bookId: string;
  progress: ReadingProgress;
  bookmarks: Bookmark[];
  annotations: Annotation[];
  quizzes: ChapterQuizRecord[];
}

export interface BookDetailResponse {
  book: BookSummary;
  toc: TocItem[];
  spine: SpineChapterSummary[];
  state: BookStudyState;
}

export interface ChapterContentResponse {
  bookId: string;
  spineIndex: number;
  title: string;
  href: string;
  html: string;
  styles: string[];
  charCount: number;
}

export interface CopilotStatusResponse {
  available: boolean;
  cliPath?: string;
  reason?: string;
}
