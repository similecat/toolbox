import React, { useState } from 'react';
import { readerApi } from '../api.js';
import type { VaultStatusResponse } from '../../shared/types.js';

interface VaultUnlockViewProps {
  status: VaultStatusResponse;
  onUnlocked: (nextStatus: VaultStatusResponse) => void;
}

export function VaultUnlockView({ status, onUnlocked }: VaultUnlockViewProps) {
  const [passphrase, setPassphrase] = useState('');
  const [confirmPassphrase, setConfirmPassphrase] = useState('');
  const [rememberOnThisPc, setRememberOnThisPc] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDpapiUnlock = async () => {
    setBusy(true);
    setError(null);
    try {
      const next = await readerApi.unlockVaultDpapi();
      onUnlocked(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Windows DPAPI unlock failed');
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (passphrase.length < 8) {
      setError('Master passphrase must be at least 8 characters.');
      return;
    }
    if (!status.initialized && passphrase !== confirmPassphrase) {
      setError('Passphrase confirmation does not match.');
      return;
    }

    setBusy(true);
    try {
      const next = status.initialized
        ? await readerApi.unlockVault(passphrase, rememberOnThisPc)
        : await readerApi.initVault(passphrase, rememberOnThisPc);
      setPassphrase('');
      setConfirmPassphrase('');
      onUnlocked(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to unlock vault');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.85rem' }}>
          <a href="/" className="btn" style={{ textDecoration: 'none', fontSize: '0.8rem', padding: '0.3rem 0.65rem' }}>
            &larr; Toolbox
          </a>
          <span className="badge badge-success">
            AES-256-GCM Encrypted Vault
          </span>
        </div>
        <div style={{ marginBottom: '1.25rem' }}>
          <h1 style={{ margin: '0 0 0.4rem', fontSize: '1.45rem' }}>
            {status.initialized ? 'Unlock Encrypted Library' : 'Initialize Encrypted Library'}
          </h1>
          <p style={{ margin: 0, fontSize: '0.9rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            {status.initialized
              ? 'All EPUB files, book titles, notes, bookmarks, and quizzes are encrypted at rest with opaque filenames.'
              : 'Create a master passphrase to encrypt imported EPUBs, titles, notes, bookmarks, and Copilot quizzes under vault/.'}
          </p>
        </div>

        {status.initialized && status.dpapiRememberEnabled && (
          <div style={{ marginBottom: '1.25rem' }}>
            <button
              type="button"
              className="btn btn-primary"
              style={{ width: '100%', padding: '0.7rem 1rem' }}
              disabled={busy}
              onClick={handleDpapiUnlock}
            >
              {busy ? 'Unlocking...' : 'Quick Unlock with Windows DPAPI'}
            </button>
            <div
              style={{
                textAlign: 'center',
                fontSize: '0.78rem',
                color: 'var(--text-muted)',
                margin: '0.85rem 0 0.25rem',
              }}
            >
              — or unlock with your master passphrase —
            </div>
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: '0.9rem' }}>
            <label className="field-label" htmlFor="vault-passphrase">
              Master Passphrase
            </label>
            <input
              id="vault-passphrase"
              type="password"
              className="text-input"
              placeholder="At least 8 characters..."
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              autoFocus={!status.dpapiRememberEnabled}
              required
            />
          </div>

          {!status.initialized && (
            <div style={{ marginBottom: '0.9rem' }}>
              <label className="field-label" htmlFor="vault-passphrase-confirm">
                Confirm Master Passphrase
              </label>
              <input
                id="vault-passphrase-confirm"
                type="password"
                className="text-input"
                placeholder="Re-enter passphrase..."
                value={confirmPassphrase}
                onChange={(e) => setConfirmPassphrase(e.target.value)}
                required
              />
            </div>
          )}

          {status.dpapiSupported && !status.dpapiRememberEnabled && (
            <label
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: '0.55rem',
                fontSize: '0.84rem',
                color: 'var(--text-secondary)',
                marginBottom: '1.1rem',
                cursor: 'pointer',
              }}
            >
              <input
                type="checkbox"
                checked={rememberOnThisPc}
                onChange={(e) => setRememberOnThisPc(e.target.checked)}
                style={{ marginTop: '0.2rem' }}
              />
              <span>
                <strong>Remember on this PC (Windows DPAPI)</strong> — wraps the vault key under your
                current Windows account while keeping all disk files encrypted and opaque.
              </span>
            </label>
          )}

          {error && (
            <div
              role="alert"
              style={{
                padding: '0.65rem 0.8rem',
                borderRadius: '8px',
                background: 'rgba(220, 38, 38, 0.1)',
                color: 'var(--danger)',
                fontSize: '0.85rem',
                marginBottom: '1rem',
              }}
            >
              {error}
            </div>
          )}

          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: '100%', padding: '0.65rem 1rem' }}
            disabled={busy}
          >
            {busy
              ? 'Processing...'
              : status.initialized
                ? 'Unlock Vault'
                : 'Create Encrypted Vault'}
          </button>
        </form>
      </div>
    </div>
  );
}
