import type {
  Annotation,
  BookDetailResponse,
  Bookmark,
  BookSummary,
  ChapterContentResponse,
  ChapterQuizRecord,
  CopilotStatusResponse,
  HighlightColor,
  ReadingProgress,
  VaultStatusResponse,
} from '../shared/types.js';

declare global {
  interface Window {
    __TOOLBOX_CSRF_TOKEN__?: string;
    __TOOLBOX_USER_EMAIL__?: string;
  }
}

const API_PREFIX = '/api/epub';

let inMemoryCsrfToken =
  typeof window !== 'undefined' && window.__TOOLBOX_CSRF_TOKEN__
    ? window.__TOOLBOX_CSRF_TOKEN__
    : '';

export function setClientCsrfToken(token: string | undefined): void {
  if (token) {
    inMemoryCsrfToken = token;
  }
}

function redirectToToolboxLoginIfSessionExpired(
  status: number,
  payload: { error?: string; code?: string } | null,
): void {
  if (status === 401 && !payload?.code && typeof window !== 'undefined') {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (inMemoryCsrfToken && init?.method && init.method !== 'GET') {
    headers.set('X-CSRF-Token', inMemoryCsrfToken);
  }
  const res = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers,
  });

  if (!res.ok) {
    let errMsg = `Request failed (${res.status})`;
    let parsedPayload: { error?: string; code?: string } | null = null;
    try {
      parsedPayload = (await res.json()) as { error?: string; code?: string };
      if (parsedPayload.error) errMsg = parsedPayload.error;
    } catch {
      // keep fallback message
    }
    redirectToToolboxLoginIfSessionExpired(res.status, parsedPayload);
    throw new Error(errMsg);
  }
  return (await res.json()) as T;
}

function parseFilenameFromContentDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      // ignore decode error
    }
  }
  const plainMatch = /filename="([^"]+)"/i.exec(header);
  return plainMatch?.[1] ?? fallback;
}

function triggerBrowserDownload(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
}

export const readerApi = {
  async getVaultStatus(): Promise<VaultStatusResponse> {
    const status = await requestJson<VaultStatusResponse>(`${API_PREFIX}/vault/status`);
    if (status.csrfToken) setClientCsrfToken(status.csrfToken);
    return status;
  },

  async initVault(passphrase: string, rememberOnThisPc: boolean): Promise<VaultStatusResponse> {
    const status = await requestJson<VaultStatusResponse>(`${API_PREFIX}/vault/init`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ passphrase, rememberOnThisPc }),
    });
    setClientCsrfToken(status.csrfToken);
    return status;
  },

  async unlockVault(passphrase: string, rememberOnThisPc: boolean): Promise<VaultStatusResponse> {
    const status = await requestJson<VaultStatusResponse>(`${API_PREFIX}/vault/unlock`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ passphrase, rememberOnThisPc }),
    });
    setClientCsrfToken(status.csrfToken);
    return status;
  },

  async unlockVaultDpapi(): Promise<VaultStatusResponse> {
    const status = await requestJson<VaultStatusResponse>(`${API_PREFIX}/vault/unlock-dpapi`, {
      method: 'POST',
    });
    setClientCsrfToken(status.csrfToken);
    return status;
  },

  async lockVault(): Promise<VaultStatusResponse> {
    const status = await requestJson<VaultStatusResponse>(`${API_PREFIX}/vault/lock`, {
      method: 'POST',
    });
    setClientCsrfToken(status.csrfToken);
    return status;
  },

  async setDpapiRemember(enabled: boolean): Promise<VaultStatusResponse> {
    return requestJson<VaultStatusResponse>(`${API_PREFIX}/vault/dpapi`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
  },

  async getCopilotStatus(): Promise<CopilotStatusResponse> {
    return requestJson<CopilotStatusResponse>(`${API_PREFIX}/copilot/status`);
  },

  async listBooks(): Promise<BookSummary[]> {
    const data = await requestJson<{ books: BookSummary[] }>(`${API_PREFIX}/books`);
    return data.books;
  },

  async importEpubFile(file: File): Promise<BookSummary> {
    const buf = await file.arrayBuffer();
    const data = await requestJson<{ book: BookSummary }>(`${API_PREFIX}/books/import`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Original-Filename': encodeURIComponent(file.name),
      },
      body: buf,
    });
    return data.book;
  },

  async importPortableVaultFile(file: File, passphrase: string): Promise<BookSummary> {
    const buf = await file.arrayBuffer();
    const utf8Bytes = new TextEncoder().encode(passphrase);
    let binary = '';
    utf8Bytes.forEach((b) => {
      binary += String.fromCharCode(b);
    });
    const base64Pass = btoa(binary);

    const data = await requestJson<{ book: BookSummary }>(`${API_PREFIX}/books/import-vault`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Vault-Passphrase': base64Pass,
      },
      body: buf,
    });
    return data.book;
  },

  async deleteBook(bookId: string): Promise<void> {
    await requestJson<{ deleted: boolean }>(`${API_PREFIX}/books/${encodeURIComponent(bookId)}`, {
      method: 'DELETE',
    });
  },

  async getBookDetail(bookId: string): Promise<BookDetailResponse> {
    return requestJson<BookDetailResponse>(`${API_PREFIX}/books/${encodeURIComponent(bookId)}`);
  },

  async getChapter(bookId: string, spineIndex: number): Promise<ChapterContentResponse> {
    return requestJson<ChapterContentResponse>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/chapters/${spineIndex}`,
    );
  },

  async saveProgress(
    bookId: string,
    payload: { spineIndex: number; scrollRatio: number; completedChapterIndex?: number },
  ): Promise<ReadingProgress> {
    const data = await requestJson<{ progress: ReadingProgress }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/progress`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    return data.progress;
  },

  async createBookmark(
    bookId: string,
    payload: {
      spineIndex: number;
      chapterTitle: string;
      scrollRatio: number;
      snippet: string;
      label: string;
    },
  ): Promise<Bookmark> {
    const data = await requestJson<{ bookmark: Bookmark }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/bookmarks`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    return data.bookmark;
  },

  async updateBookmark(bookId: string, bookmarkId: string, label: string): Promise<Bookmark> {
    const data = await requestJson<{ bookmark: Bookmark }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/bookmarks/${encodeURIComponent(bookmarkId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label }),
      },
    );
    return data.bookmark;
  },

  async deleteBookmark(bookId: string, bookmarkId: string): Promise<void> {
    await requestJson(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/bookmarks/${encodeURIComponent(bookmarkId)}`,
      { method: 'DELETE' },
    );
  },

  async createAnnotation(
    bookId: string,
    payload: {
      spineIndex: number;
      chapterTitle: string;
      startOffset: number;
      endOffset: number;
      selectedText: string;
      prefix: string;
      suffix: string;
      color: HighlightColor;
      noteMarkdown: string;
    },
  ): Promise<Annotation> {
    const data = await requestJson<{ annotation: Annotation }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/annotations`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    return data.annotation;
  },

  async updateAnnotation(
    bookId: string,
    annotationId: string,
    payload: { color?: HighlightColor; noteMarkdown?: string },
  ): Promise<Annotation> {
    const data = await requestJson<{ annotation: Annotation }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/annotations/${encodeURIComponent(annotationId)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    return data.annotation;
  },

  async deleteAnnotation(bookId: string, annotationId: string): Promise<void> {
    await requestJson(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/annotations/${encodeURIComponent(annotationId)}`,
      { method: 'DELETE' },
    );
  },

  async generateChapterQuiz(
    bookId: string,
    spineIndex: number,
    opts?: { mcqCount?: number; shortAnswerCount?: number },
  ): Promise<ChapterQuizRecord> {
    const data = await requestJson<{ quiz: ChapterQuizRecord }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/chapters/${spineIndex}/quiz/generate`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(opts ?? {}),
      },
    );
    return data.quiz;
  },

  async submitQuiz(
    bookId: string,
    quizId: string,
    answers: Record<
      string,
      { type: 'mcq'; selectedIndex: number } | { type: 'short_answer'; answerText: string }
    >,
  ): Promise<{ quiz: ChapterQuizRecord; progress: ReadingProgress }> {
    return requestJson<{ quiz: ChapterQuizRecord; progress: ReadingProgress }>(
      `${API_PREFIX}/books/${encodeURIComponent(bookId)}/quizzes/${encodeURIComponent(quizId)}/submit`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers }),
      },
    );
  },

  async downloadExportEpub(bookId: string): Promise<void> {
    const res = await fetch(`${API_PREFIX}/books/${encodeURIComponent(bookId)}/export/epub`, {
      credentials: 'same-origin',
    });
    if (!res.ok) throw new Error('Failed to export decrypted EPUB');
    const blob = await res.blob();
    const filename = parseFilenameFromContentDisposition(
      res.headers.get('content-disposition'),
      'exported-book.epub',
    );
    triggerBrowserDownload(blob, filename);
  },

  async downloadExportMarkdown(bookId: string): Promise<void> {
    const res = await fetch(`${API_PREFIX}/books/${encodeURIComponent(bookId)}/export/markdown`, {
      credentials: 'same-origin',
    });
    if (!res.ok) throw new Error('Failed to export study notes Markdown');
    const blob = await res.blob();
    const filename = parseFilenameFromContentDisposition(
      res.headers.get('content-disposition'),
      'study-notes.md',
    );
    triggerBrowserDownload(blob, filename);
  },

  async downloadExportVault(bookId: string, exportPassphrase: string): Promise<void> {
    const res = await fetch(`${API_PREFIX}/books/${encodeURIComponent(bookId)}/export/vault`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': inMemoryCsrfToken,
      },
      body: JSON.stringify({ exportPassphrase }),
    });
    if (!res.ok) {
      let msg = 'Failed to export encrypted vault backup';
      let parsed: { error?: string; code?: string } | null = null;
      try {
        parsed = (await res.json()) as { error?: string; code?: string };
        if (parsed.error) msg = parsed.error;
      } catch {
        // ignore
      }
      redirectToToolboxLoginIfSessionExpired(res.status, parsed);
      throw new Error(msg);
    }
    const blob = await res.blob();
    const filename = parseFilenameFromContentDisposition(
      res.headers.get('content-disposition'),
      `encrypted-book-${bookId.slice(0, 8)}.vault`,
    );
    triggerBrowserDownload(blob, filename);
  },
};
