/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

import type {ChunkSelectState} from './stackEdit/chunkSelectState';

import {T, t} from './i18n';
import {Set as ImSet, Range} from 'immutable';
import {useRef, useState} from 'react';

import './PartialFileSelection.css';

export function PartialFileSelection(props: {
  chunkSelection: ChunkSelectState;
  setChunkSelection: (state: ChunkSelectState) => void;
}) {
  // States for context line expansion.
  const [expandedALines, setExpandedALines] = useState<ImSet<number>>(ImSet);

  // State for range selection.
  const lastLine = useRef(-1);

  const lines = props.chunkSelection.getLines();

  const handlePointerDown = (
    lineIdx: number,
    chunk = false,
    e: React.PointerEvent<HTMLDivElement>,
  ) => {
    const line = lines[lineIdx];
    if (e.isPrimary && line.selected !== null) {
      const selected = !line.selected;
      const lineSelects: Array<[number, boolean]> = [];
      if (chunk) {
        // "Flood fill" surrounding lines with the same selection state.
        for (let i = lineIdx + 1; i < lines.length && lines[i].selected === line.selected; i++) {
          lineSelects.push([lines[i].rawIndex, selected]);
        }
        for (let i = lineIdx; i >= 0 && lines[i].selected === line.selected; i--) {
          lineSelects.push([lines[i].rawIndex, selected]);
        }
      } else {
        lineSelects.push([line.rawIndex, selected]);
      }
      const newSelection = props.chunkSelection.setSelectedLines(lineSelects);
      lastLine.current = lineIdx;
      props.setChunkSelection(newSelection);
    }
  };

  const handlePointerEnter = (lineIdx: number, e: React.PointerEvent<HTMLDivElement>) => {
    const line = lines[lineIdx];
    if (e.buttons === 1 && line.selected !== null && lastLine.current !== lineIdx) {
      const newSelection = props.chunkSelection.setSelectedLines([[line.rawIndex, !line.selected]]);
      lastLine.current = lineIdx;
      props.setChunkSelection(newSelection);
    }
  };

  // Skip unchanged lines.
  const contextCount = 2;
  const skipLines = Array<boolean>(lines.length + contextCount).fill(true);
  lines.forEach((line, i) => {
    if (line.sign !== '' || expandedALines.has(line.aLine ?? -1)) {
      for (let j = i + contextCount; j >= 0 && j >= i - contextCount && skipLines[j]; j--) {
        skipLines[j] = false;
      }
    }
  });

  // Render the rows.
  const lineAContent: JSX.Element[] = [];
  const lineBContent: JSX.Element[] = [];
  const lineANumber: JSX.Element[] = [];
  const lineBNumber: JSX.Element[] = [];

  let skipping = false;
  let skipStart = -1;

  const insertContextLines = (i: number) => {
    // Capture `skipStart` and `i` in a local variable inside loop body.
    const skipRange = Range(skipStart, i);
    const handleExpand = () => {
      // Only the "unchanged" lines need expansion.
      // We use line numbers on the "a" side, which remains "stable" regardless of editing.
      const newLines = skipRange.map(j => lines[j].aLine ?? -1).filter(i => i >= 0);
      const newSet = expandedALines.union(newLines);
      setExpandedALines(newSet);
    };
    const contextLineContent = (
      <div
        key={i - 1}
        className="line line-context"
        title={t('Click to expand lines.')}
        onClick={handleExpand}
      />
    );
    lineAContent.push(contextLineContent);
    lineBContent.push(contextLineContent);
    const contextLineNumber = <div key={i - 1} className="line-number line-context" />;
    lineANumber.push(contextLineNumber);
    lineBNumber.push(contextLineNumber);
  };

  lines.forEach((line, i) => {
    if (skipLines[i]) {
      if (!skipping) {
        skipStart = i;
      }
      skipping = true;
      return;
    }
    let rowClass =
      line.selected === null ? 'unselectable' : line.selected ? 'selected ' : 'deselected';

    // Draw "~~~" between chunks.
    if (skipping) {
      insertContextLines(i);
      skipping = false;
    }

    // Draw the actual line and line numbers.
    let aLineData = line.data;
    let bLineData = line.data;
    if (line.sign === '+' || line.sign === '!+') {
      if (line.selected) {
        aLineData = '\n';
      } else {
        bLineData = '\n';
      }
      rowClass += ' line-add';
    }
    if (line.sign === '-' || line.sign === '!-') {
      if (line.selected) {
        bLineData = '\n';
      }
      rowClass += ' line-del';
    }

    const lineNumberProps = {
      onPointerDown: handlePointerDown.bind(null, i, false),
      onPointerEnter: handlePointerEnter.bind(null, i),
      title:
        line.selected == null
          ? undefined
          : t('Click to toggle line selection. Drag for range selection.'),
    };

    const lineContentProps = {
      onPointerDown: handlePointerDown.bind(null, i, true),
      title: line.selected == null ? undefined : t('Click to toggle chunk selection.'),
    };

    lineAContent.push(
      <div key={i} className={`line line-a ${rowClass}`} {...lineContentProps}>
        {aLineData}
      </div>,
    );
    lineBContent.push(
      <div key={i} className={`line line-b ${rowClass}`}>
        {bLineData}
      </div>,
    );

    lineANumber.push(
      <div key={i} className={`line-number line-a ${rowClass}`} {...lineNumberProps}>
        {line.aLine ?? '\n'}
      </div>,
    );
    lineBNumber.push(
      <div key={i} className={`line-number line-b ${rowClass}`} {...lineNumberProps}>
        {line.selLine ?? '\n'}
      </div>,
    );
  });

  if (skipping) {
    insertContextLines(lines.length);
    skipping = false;
  }

  return (
    <div className="partial-file-selection-width-min-content">
      <div className="partial-file-selection-scroll-y">
        <div className="partial-file-selection">
          <div className="partial-file-selection-scroll-x">
            <pre className="column-a">{lineAContent}</pre>
          </div>
          <pre className="column-a-number">{lineANumber}</pre>
          <pre className="column-b-number">{lineBNumber}</pre>
          <div className="partial-file-selection-scroll-x">
            <pre className="column-b">{lineBContent}</pre>
          </div>
        </div>
      </div>
      <div className="partial-file-selection-tip">
        <T>Click lines on the left side, or line numbers to toggle selection. </T>
        <T>The right side shows the selection result.</T>
      </div>
    </div>
  );
}
