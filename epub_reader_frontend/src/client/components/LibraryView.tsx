import React, { useRef, useState } from 'react';
import { readerApi } from '../api.js';
import type {
  BookSummary,
  CopilotStatusResponse,
  ReaderTheme,
  VaultStatusResponse,
} from '../../shared/types.js';

interface LibraryViewProps {
  books: BookSummary[];
  vaultStatus: VaultStatusResponse;
  copilotStatus: CopilotStatusResponse | null;
  theme: ReaderTheme;
  onChangeTheme: (theme: ReaderTheme) => void;
  onRefreshBooks: () => Promise<void>;
  onVaultStatusChange: (status: VaultStatusResponse) => void;
  onOpenBook: (bookId: string) => void;
  onLockVault: () => Promise<void>;
}

export function LibraryView({
  books,
  vaultStatus,
  copilotStatus,
  theme,
  onChangeTheme,
  onRefreshBooks,
  onVaultStatusChange,
  onOpenBook,
  onLockVault,
}: LibraryViewProps) {
  const epubInputRef = useRef<HTMLInputElement | null>(null);
  const vaultInputRef = useRef<HTMLInputElement | null>(null);

  const [search, setSearch] = useState('');
  const [busyMessage, setBusyMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Modal state for portable .vault export or import passphrase
  const [vaultExportBook, setVaultExportBook] = useState<BookSummary | null>(null);
  const [pendingVaultImportFile, setPendingVaultImportFile] = useState<File | null>(null);
  const [transferPassphrase, setTransferPassphrase] = useState('');
  const [openMenuBookId, setOpenMenuBookId] = useState<string | null>(null);

  const filteredBooks = books.filter((b) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return b.title.toLowerCase().includes(q) || b.author.toLowerCase().includes(q);
  });

  const handleImportEpub = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setError(null);
    setBusyMessage('Encrypting and importing EPUB...');
    try {
      await readerApi.importEpubFile(file);
      await onRefreshBooks();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import EPUB');
    } finally {
      setBusyMessage(null);
    }
  };

  const handleSelectVaultFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setTransferPassphrase('');
    setPendingVaultImportFile(file);
  };

  const confirmImportVault = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!pendingVaultImportFile) return;
    setError(null);
    setBusyMessage('Decrypting and importing .vault package...');
    try {
      await readerApi.importPortableVaultFile(pendingVaultImportFile, transferPassphrase);
      setPendingVaultImportFile(null);
      setTransferPassphrase('');
      await onRefreshBooks();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import .vault backup');
    } finally {
      setBusyMessage(null);
    }
  };

  const confirmExportVault = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!vaultExportBook) return;
    setError(null);
    setBusyMessage('Generating encrypted .vault package...');
    try {
      await readerApi.downloadExportVault(vaultExportBook.bookId, transferPassphrase);
      setVaultExportBook(null);
      setTransferPassphrase('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to export .vault package');
    } finally {
      setBusyMessage(null);
    }
  };

  const handleToggleDpapi = async () => {
    setError(null);
    try {
      const next = await readerApi.setDpapiRemember(!vaultStatus.dpapiRememberEnabled);
      onVaultStatusChange(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update Windows DPAPI setting');
    }
  };

  const handleDeleteBook = async (book: BookSummary) => {
    const confirmed = window.confirm(
      `Permanently shred encrypted book "${book.title}" and all its notes, bookmarks, and quiz history?`,
    );
    if (!confirmed) return;
    setError(null);
    setBusyMessage('Securely erasing encrypted objects...');
    try {
      await readerApi.deleteBook(book.bookId);
      await onRefreshBooks();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete book');
    } finally {
      setBusyMessage(null);
    }
  };

  return (
    <div className="library-shell">
      <header className="library-header">
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap' }}>
            <a href="/" className="btn" style={{ textDecoration: 'none', padding: '0.35rem 0.7rem' }}>
              &larr; Toolbox
            </a>
            <h1 style={{ margin: 0, fontSize: '1.5rem' }}>Encrypted EPUB Library</h1>
            <span className="badge badge-success">Vault Unlocked</span>
            {copilotStatus && (
              <span
                className={`badge ${copilotStatus.available ? 'badge-success' : 'badge-warn'}`}
                title={copilotStatus.reason || copilotStatus.cliPath || 'GitHub Copilot SDK'}
              >
                {copilotStatus.available ? 'Copilot SDK Ready' : 'Copilot SDK Offline'}
              </span>
            )}
          </div>
          <p style={{ margin: '0.3rem 0 0', fontSize: '0.86rem', color: 'var(--text-secondary)' }}>
            Books, titles, notes, bookmarks, and quizzes are stored encrypted with random IDs under{' '}
            <code>vault/</code>.
          </p>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.55rem', flexWrap: 'wrap' }}>
          <input
            ref={epubInputRef}
            type="file"
            accept=".epub,application/epub+zip"
            style={{ display: 'none' }}
            onChange={handleImportEpub}
          />
          <input
            ref={vaultInputRef}
            type="file"
            accept=".vault,application/octet-stream"
            style={{ display: 'none' }}
            onChange={handleSelectVaultFile}
          />

          <button
            type="button"
            className="btn btn-primary"
            disabled={Boolean(busyMessage)}
            onClick={() => epubInputRef.current?.click()}
          >
            + Import EPUB
          </button>
          <button
            type="button"
            className="btn"
            disabled={Boolean(busyMessage)}
            onClick={() => vaultInputRef.current?.click()}
          >
            Import .vault
          </button>

          <select
            aria-label="Theme"
            className="btn"
            value={theme}
            onChange={(e) => onChangeTheme(e.target.value as ReaderTheme)}
          >
            <option value="light">Light</option>
            <option value="sepia">Sepia</option>
            <option value="dark">Dark</option>
          </select>

          {vaultStatus.dpapiSupported && (
            <button
              type="button"
              className="btn"
              onClick={handleToggleDpapi}
              title="Toggle Windows DPAPI quick unlock on this PC"
            >
              {vaultStatus.dpapiRememberEnabled ? 'DPAPI: On' : 'DPAPI: Off'}
            </button>
          )}

          <button type="button" className="btn btn-danger" onClick={onLockVault}>
            Lock Vault
          </button>
        </div>
      </header>

      <div style={{ marginBottom: '1.25rem', display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
        <input
          type="search"
          className="text-input"
          style={{ maxWidth: '380px' }}
          placeholder="Search decrypted titles or authors in memory..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {busyMessage && <span className="badge">{busyMessage}</span>}
      </div>

      {error && (
        <div
          role="alert"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: '10px',
            background: 'rgba(220, 38, 38, 0.1)',
            color: 'var(--danger)',
            marginBottom: '1.25rem',
            fontSize: '0.88rem',
          }}
        >
          {error}
        </div>
      )}

      {filteredBooks.length === 0 ? (
        <div
          style={{
            textAlign: 'center',
            padding: '4rem 1.5rem',
            background: 'var(--bg-surface)',
            borderRadius: '16px',
            border: '1px dashed var(--border)',
          }}
        >
          <h2 style={{ margin: '0 0 0.5rem', fontSize: '1.2rem' }}>
            {books.length === 0 ? 'Your encrypted library is empty' : 'No matching books'}
          </h2>
          <p style={{ margin: '0 0 1.25rem', color: 'var(--text-secondary)', fontSize: '0.92rem' }}>
            {books.length === 0
              ? 'Import any .epub file to encrypt its filename, metadata, and chapters inside your local vault.'
              : 'Try a different search keyword.'}
          </p>
          {books.length === 0 && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => epubInputRef.current?.click()}
            >
              Import Your First EPUB
            </button>
          )}
        </div>
      ) : (
        <div className="books-grid">
          {filteredBooks.map((book) => {
            const chapterPct = Math.round(
              ((book.progress.spineIndex + book.progress.scrollRatio) /
                Math.max(1, book.chapterCount)) *
                100,
            );
            const isMenuOpen = openMenuBookId === book.bookId;

            return (
              <article key={book.bookId} className="book-card">
                <div className="book-card-top">
                  {book.coverDataUrl ? (
                    <img
                      src={book.coverDataUrl}
                      alt=""
                      className="book-cover"
                    />
                  ) : (
                    <div className="book-cover">{book.title.slice(0, 2).toUpperCase()}</div>
                  )}

                  <div style={{ flex: 1, minWidth: 0 }}>
                    <h3
                      style={{
                        margin: '0 0 0.25rem',
                        fontSize: '1rem',
                        lineHeight: 1.35,
                        display: '-webkit-box',
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: 'vertical',
                        overflow: 'hidden',
                      }}
                      title={book.title}
                    >
                      {book.title}
                    </h3>
                    <div
                      style={{
                        fontSize: '0.83rem',
                        color: 'var(--text-secondary)',
                        marginBottom: '0.5rem',
                        whiteSpace: 'nowrap',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      {book.author}
                    </div>
                    <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
                      <span className="badge">{book.chapterCount} ch</span>
                      <span className="badge">{book.language}</span>
                      {book.progress.completedChapters.length > 0 && (
                        <span className="badge badge-success">
                          {book.progress.completedChapters.length} quizzed
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                <div style={{ padding: '0 1.1rem 0.9rem', marginTop: 'auto' }}>
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      fontSize: '0.76rem',
                      color: 'var(--text-muted)',
                      marginBottom: '0.35rem',
                    }}
                  >
                    <span>
                      Ch {book.progress.spineIndex + 1} / {book.chapterCount}
                    </span>
                    <span>{Math.min(100, Math.max(0, chapterPct))}%</span>
                  </div>
                  <div className="progress-track" style={{ marginBottom: '0.9rem' }}>
                    <div
                      className="progress-fill"
                      style={{ width: `${Math.min(100, Math.max(0, chapterPct))}%` }}
                    />
                  </div>

                  <div style={{ display: 'flex', gap: '0.5rem', position: 'relative' }}>
                    <button
                      type="button"
                      className="btn btn-primary"
                      style={{ flex: 1 }}
                      onClick={() => onOpenBook(book.bookId)}
                    >
                      Read
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => setOpenMenuBookId(isMenuOpen ? null : book.bookId)}
                    >
                      Export ▾
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger"
                      title="Securely delete book"
                      onClick={() => handleDeleteBook(book)}
                    >
                      ✕
                    </button>

                    {isMenuOpen && (
                      <div
                        style={{
                          position: 'absolute',
                          right: 0,
                          bottom: '42px',
                          background: 'var(--bg-surface)',
                          border: '1px solid var(--border)',
                          borderRadius: '10px',
                          boxShadow: 'var(--shadow)',
                          padding: '0.4rem',
                          minWidth: '225px',
                          zIndex: 30,
                          display: 'flex',
                          flexDirection: 'column',
                          gap: '0.25rem',
                        }}
                      >
                        <button
                          type="button"
                          className="btn"
                          style={{ justifyContent: 'flex-start', border: 'none' }}
                          onClick={async () => {
                            setOpenMenuBookId(null);
                            try {
                              await readerApi.downloadExportEpub(book.bookId);
                            } catch (err) {
                              setError(err instanceof Error ? err.message : 'EPUB export failed');
                            }
                          }}
                        >
                          Export Original .epub
                        </button>
                        <button
                          type="button"
                          className="btn"
                          style={{ justifyContent: 'flex-start', border: 'none' }}
                          onClick={async () => {
                            setOpenMenuBookId(null);
                            try {
                              await readerApi.downloadExportMarkdown(book.bookId);
                            } catch (err) {
                              setError(err instanceof Error ? err.message : 'Markdown export failed');
                            }
                          }}
                        >
                          Export Notes & Quizzes (.md)
                        </button>
                        <button
                          type="button"
                          className="btn"
                          style={{ justifyContent: 'flex-start', border: 'none' }}
                          onClick={() => {
                            setOpenMenuBookId(null);
                            setTransferPassphrase('');
                            setVaultExportBook(book);
                          }}
                        >
                          Export Encrypted Backup (.vault)
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {/* Portable .vault Export / Import Passphrase Dialog */}
      {(vaultExportBook || pendingVaultImportFile) && (
        <div className="modal-backdrop">
          <div className="modal-panel" style={{ maxWidth: '440px' }}>
            <h2 style={{ margin: '0 0 0.5rem', fontSize: '1.2rem' }}>
              {vaultExportBook
                ? 'Export Encrypted .vault Package'
                : 'Unlock Portable .vault Package'}
            </h2>
            <p style={{ margin: '0 0 1rem', fontSize: '0.88rem', color: 'var(--text-secondary)' }}>
              {vaultExportBook
                ? 'Choose a passphrase to encrypt this portable archive (includes EPUB bytes, notes, bookmarks, and quiz history with zero plaintext metadata).'
                : `Enter the passphrase used when "${pendingVaultImportFile?.name}" was exported.`}
            </p>
            <form onSubmit={vaultExportBook ? confirmExportVault : confirmImportVault}>
              <label className="field-label" htmlFor="transfer-passphrase">
                Archive Passphrase (min 8 characters)
              </label>
              <input
                id="transfer-passphrase"
                type="password"
                className="text-input"
                value={transferPassphrase}
                onChange={(e) => setTransferPassphrase(e.target.value)}
                minLength={8}
                required
                autoFocus
              />
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'flex-end',
                  gap: '0.6rem',
                  marginTop: '1.2rem',
                }}
              >
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setVaultExportBook(null);
                    setPendingVaultImportFile(null);
                    setTransferPassphrase('');
                  }}
                >
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={Boolean(busyMessage)}>
                  {vaultExportBook ? 'Download .vault' : 'Decrypt & Import'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
