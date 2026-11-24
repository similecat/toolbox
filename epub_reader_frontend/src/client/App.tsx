import React, { useCallback, useEffect, useState } from 'react';
import { readerApi } from './api.js';
import { LibraryView } from './components/LibraryView.js';
import { ReaderView } from './components/ReaderView.js';
import { VaultUnlockView } from './components/VaultUnlockView.js';
import type {
  BookSummary,
  CopilotStatusResponse,
  ReaderTheme,
  VaultStatusResponse,
} from '../shared/types.js';

export function App() {
  const [vaultStatus, setVaultStatus] = useState<VaultStatusResponse | null>(null);
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [copilotStatus, setCopilotStatus] = useState<CopilotStatusResponse | null>(null);
  const [activeBookId, setActiveBookId] = useState<string | null>(null);
  const [theme, setTheme] = useState<ReaderTheme>('light');
  const [bootError, setBootError] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const refreshBooks = useCallback(async () => {
    const list = await readerApi.listBooks();
    setBooks(list);
  }, []);

  const loadUnlockedWorkspace = useCallback(async () => {
    await refreshBooks();
    void readerApi
      .getCopilotStatus()
      .then((s) => setCopilotStatus(s))
      .catch(() => setCopilotStatus({ available: false, reason: 'Status check failed' }));
  }, [refreshBooks]);

  useEffect(() => {
    (async () => {
      try {
        const status = await readerApi.getVaultStatus();
        setVaultStatus(status);
        if (status.unlocked) {
          await loadUnlockedWorkspace();
        }
      } catch (err) {
        setBootError(err instanceof Error ? err.message : 'Unable to reach Toolbox EPUB reader service');
      }
    })();
  }, [loadUnlockedWorkspace]);

  const handleLockVault = async () => {
    try {
      const next = await readerApi.lockVault();
      setActiveBookId(null);
      setBooks([]);
      setVaultStatus(next);
    } catch {
      setActiveBookId(null);
      setBooks([]);
      setVaultStatus((prev) => (prev ? { ...prev, unlocked: false } : prev));
    }
  };

  if (bootError) {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <h2 style={{ marginTop: 0 }}>Connection Error</h2>
          <p style={{ color: 'var(--text-secondary)' }}>{bootError}</p>
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!vaultStatus) {
    return (
      <div className="auth-shell">
        <div style={{ color: 'var(--text-muted)' }}>Connecting to encrypted vault...</div>
      </div>
    );
  }

  if (!vaultStatus.unlocked) {
    return (
      <VaultUnlockView
        status={vaultStatus}
        onUnlocked={async (nextStatus) => {
          setVaultStatus(nextStatus);
          await loadUnlockedWorkspace();
        }}
      />
    );
  }

  if (activeBookId) {
    return (
      <ReaderView
        bookId={activeBookId}
        copilotStatus={copilotStatus}
        theme={theme}
        onChangeTheme={setTheme}
        onBackToLibrary={async () => {
          setActiveBookId(null);
          await refreshBooks();
        }}
        onLockVault={handleLockVault}
      />
    );
  }

  return (
    <LibraryView
      books={books}
      vaultStatus={vaultStatus}
      copilotStatus={copilotStatus}
      theme={theme}
      onChangeTheme={setTheme}
      onRefreshBooks={refreshBooks}
      onVaultStatusChange={setVaultStatus}
      onOpenBook={(id) => setActiveBookId(id)}
      onLockVault={handleLockVault}
    />
  );
}
