export interface TextSelectorTarget {
  startOffset: number;
  endOffset: number;
  selectedText: string;
  prefix: string;
  suffix: string;
}

export function resolveTextPositionRange(
  fullText: string,
  target: TextSelectorTarget,
): { start: number; end: number } | null {
  const { startOffset, endOffset, selectedText, prefix, suffix } = target;
  if (!selectedText) return null;

  // 1. Exact offset match
  if (
    startOffset >= 0 &&
    endOffset <= fullText.length &&
    fullText.slice(startOffset, endOffset) === selectedText
  ) {
    return { start: startOffset, end: endOffset };
  }

  // 2. Context-anchored match (prefix + selectedText + suffix)
  if (prefix || suffix) {
    const composite = `${prefix}${selectedText}${suffix}`;
    const foundComposite = fullText.indexOf(composite);
    if (foundComposite >= 0) {
      const start = foundComposite + prefix.length;
      return { start, end: start + selectedText.length };
    }
  }

  // 3. Nearest exact quote match to original startOffset
  let bestIdx = -1;
  let bestDist = Number.POSITIVE_INFINITY;
  let searchFrom = 0;
  while (searchFrom <= fullText.length - selectedText.length) {
    const idx = fullText.indexOf(selectedText, searchFrom);
    if (idx === -1) break;
    const dist = Math.abs(idx - startOffset);
    if (dist < bestDist) {
      bestDist = dist;
      bestIdx = idx;
    }
    searchFrom = idx + 1;
  }

  if (bestIdx >= 0) {
    return { start: bestIdx, end: bestIdx + selectedText.length };
  }
  return null;
}

export function captureSelectionAnchor(
  rootEl: HTMLElement,
  selection: Selection,
): TextSelectorTarget | null {
  if (selection.isCollapsed || selection.rangeCount === 0) {
    return null;
  }
  const range = selection.getRangeAt(0);
  if (!rootEl.contains(range.commonAncestorContainer)) {
    return null;
  }
  const selectedText = range.toString();
  if (!selectedText.trim()) {
    return null;
  }

  const preRange = document.createRange();
  preRange.selectNodeContents(rootEl);
  preRange.setEnd(range.startContainer, range.startOffset);
  const startOffset = preRange.toString().length;
  const endOffset = startOffset + selectedText.length;

  const fullText = rootEl.textContent ?? '';
  const prefix = fullText.slice(Math.max(0, startOffset - 32), startOffset);
  const suffix = fullText.slice(endOffset, Math.min(fullText.length, endOffset + 32));

  return {
    startOffset,
    endOffset,
    selectedText,
    prefix,
    suffix,
  };
}

export interface DomHighlightSpec extends TextSelectorTarget {
  id: string;
  color: string;
  hasNote: boolean;
}

export function applyHighlightsToDom(
  rootEl: HTMLElement,
  highlights: DomHighlightSpec[],
): void {
  if (highlights.length === 0) return;
  const fullText = rootEl.textContent ?? '';
  if (!fullText) return;

  const resolved = highlights
    .map((h) => {
      const pos = resolveTextPositionRange(fullText, h);
      return pos ? { ...h, start: pos.start, end: pos.end } : null;
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)
    .sort((a, b) => a.start - b.start || b.end - a.end);

  if (resolved.length === 0) return;

  // Collect all text nodes with their global character offsets once
  const walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT);
  const textNodes: Array<{ node: Text; start: number; end: number }> = [];
  let cursor = 0;
  let current: Node | null;
  while ((current = walker.nextNode()) !== null) {
    const textNode = current as Text;
    const len = textNode.nodeValue?.length ?? 0;
    if (len > 0) {
      textNodes.push({ node: textNode, start: cursor, end: cursor + len });
      cursor += len;
    }
  }

  // Apply from back to front so splitting earlier text nodes doesn't invalidate unprocessed nodes
  for (let i = resolved.length - 1; i >= 0; i -= 1) {
    const hl = resolved[i]!;
    for (let nIdx = textNodes.length - 1; nIdx >= 0; nIdx -= 1) {
      const entry = textNodes[nIdx]!;
      if (entry.end <= hl.start || entry.start >= hl.end) {
        continue;
      }
      const parent = entry.node.parentElement;
      if (!parent || parent.closest('mark.epub-hl')) {
        continue;
      }
      const localStart = Math.max(0, hl.start - entry.start);
      const localEnd = Math.min(entry.end - entry.start, hl.end - entry.start);
      if (localEnd <= localStart) continue;

      let targetNode = entry.node;
      if (localEnd < (targetNode.nodeValue?.length ?? 0)) {
        targetNode.splitText(localEnd);
      }
      if (localStart > 0) {
        targetNode = targetNode.splitText(localStart);
      }

      const mark = document.createElement('mark');
      mark.className = `epub-hl epub-hl-${hl.color}${hl.hasNote ? ' epub-hl-noted' : ''}`;
      mark.dataset.annotationId = hl.id;
      parent.replaceChild(mark, targetNode);
      mark.appendChild(targetNode);
    }
  }
}
