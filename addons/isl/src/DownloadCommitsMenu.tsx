/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

import type {CommitInfo} from './types';

import {globalRecoil} from './AccessGlobalRecoil';
import {CommitCloudInfo} from './CommitCloud';
import {DropdownFields} from './DropdownFields';
import {Internal} from './Internal';
import {Tooltip} from './Tooltip';
import {VSCodeCheckbox} from './VSCodeCheckbox';
import {t, T} from './i18n';
import {GotoOperation} from './operations/GotoOperation';
import {GraftOperation} from './operations/GraftOperation';
import {PullRevOperation} from './operations/PullRevOperation';
import {RebaseKeepOperation} from './operations/RebaseKeepOperation';
import {RebaseOperation} from './operations/RebaseOperation';
import {persistAtomToConfigEffect} from './persistAtomToConfigEffect';
import {treeWithPreviews} from './previews';
import {forceFetchCommit, useRunOperation} from './serverAPIState';
import {succeedableRevset, exactRevset} from './types';
import {VSCodeButton, VSCodeDivider, VSCodeTextField} from '@vscode/webview-ui-toolkit/react';
import {useEffect, useRef, useState} from 'react';
import {atom, useRecoilState} from 'recoil';
import {Icon} from 'shared/Icon';
import {unwrap} from 'shared/utils';

import './DownloadCommitsMenu.css';

export function DownloadCommitsTooltipButton() {
  return (
    <Tooltip
      trigger="click"
      component={dismiss => <DownloadCommitsTooltip dismiss={dismiss} />}
      placement="bottom"
      title={t('Download commits')}>
      <VSCodeButton appearance="icon" data-testid="download-commits-tooltip-button">
        <Icon icon="cloud-download" />
      </VSCodeButton>
    </Tooltip>
  );
}

function findCurrentPublicBase(): CommitInfo | undefined {
  const tree = globalRecoil().getLoadable(treeWithPreviews).valueMaybe();
  let commit = tree?.headCommit;
  while (commit) {
    if (commit.phase === 'public') {
      return commit;
    }
    commit = tree?.treeMap.get(commit.parents[0])?.info;
  }
  return undefined;
}

const downloadCommitRebaseType = atom<'rebase_base' | 'rebase_ontop' | null>({
  key: 'downloadCommitRebaseType',
  default: null,
  effects: [
    persistAtomToConfigEffect<'rebase_base' | 'rebase_ontop' | null>(
      'isl.download-commit-rebase-type',
      null,
    ),
  ],
});

const downloadCommitShouldGoto = atom({
  key: 'downloadCommitShouldGoto',
  default: false,
  effects: [persistAtomToConfigEffect('isl.download-commit-should-goto', false as boolean)],
});

function DownloadCommitsTooltip({dismiss}: {dismiss: () => unknown}) {
  const [enteredDiffNum, setEnteredDiffNum] = useState('');
  const runOperation = useRunOperation();
  const supportsDiffDownload = Internal.diffDownloadOperation != null;
  const downloadDiffTextArea = useRef(null);
  useEffect(() => {
    if (downloadDiffTextArea.current) {
      (downloadDiffTextArea.current as HTMLTextAreaElement).focus();
    }
  }, [downloadDiffTextArea]);

  const [rebaseType, setRebaseType] = useRecoilState(downloadCommitRebaseType);
  const [shouldGoto, setShouldGoto] = useRecoilState(downloadCommitShouldGoto);

  const doCommitDownload = async () => {
    // We need to dismiss the tooltip now, since we don't want to leave it up until after the operations are run.
    dismiss();

    // Typically, we'd just immediately use runOperation to queue up additional operations.
    // Unfortunately, we don't know if the result will be public or not,
    // and that changes how we'll rebase/graft the result.  This means we can't use the queueing system.
    // This is not a correctness issue because we show no optimistically downloaded result to act on.
    // Worst case, the rebase/goto will be queued after some other unrelated actions which should be fine.

    // TODO: don't use different internal download operation once phrevset is supported in SL_AUTOMATION_EXCEPT.
    if (Internal.diffDownloadOperation != null) {
      await runOperation(Internal.diffDownloadOperation(enteredDiffNum));
    } else {
      await runOperation(new PullRevOperation(enteredDiffNum));
    }

    // Lookup the result of the pull
    const latest = await forceFetchCommit(enteredDiffNum).catch(() => null);
    if (!latest) {
      // We can't continue with the rebase/goto if the lookup failed.
      return;
    }

    // Now we CAN queue up additional actions

    const isPublic = latest?.phase === 'public';
    if (rebaseType != null) {
      const Op = isPublic
        ? // "graft" implicitly does "goto", "rebase --keep" does not
          shouldGoto
          ? GraftOperation
          : RebaseKeepOperation
        : RebaseOperation;
      const dest = rebaseType === 'rebase_ontop' ? '.' : unwrap(findCurrentPublicBase()?.hash);
      // Use exact revsets for sources, so that you can type a specific hash to download and not be surprised by succession.
      // Only use succession for destination, which may be in flux at the moment you start the download.
      runOperation(new Op(exactRevset(enteredDiffNum), succeedableRevset(dest)));
    }

    if (
      shouldGoto &&
      // Goto for public commits will be handled by Graft.
      // Goto on max(latest_successors(revset)) would just yield the existing public commit.
      !isPublic
    ) {
      runOperation(new GotoOperation(exactRevset(enteredDiffNum)));
    }
  };

  return (
    <DropdownFields
      title={<T>Download Commits</T>}
      icon="cloud-download"
      data-testid="download-commits-dropdown">
      <div className="download-commits-content">
        <div className="download-commits-input-row">
          <VSCodeTextField
            placeholder={
              supportsDiffDownload ? t('Hash, Diff Number, ...') : t('Hash, revset, ...')
            }
            value={enteredDiffNum}
            data-testid="download-commits-input"
            onInput={e => setEnteredDiffNum((e.target as unknown as {value: string})?.value ?? '')}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                if (enteredDiffNum.trim().length > 0) {
                  doCommitDownload();
                }
              }
            }}
            ref={downloadDiffTextArea}
          />
          <VSCodeButton
            appearance="secondary"
            data-testid="download-commit-button"
            disabled={enteredDiffNum.trim().length === 0}
            onClick={doCommitDownload}>
            <T>Pull</T>
          </VSCodeButton>
        </div>
        <div className="download-commits-input-row">
          <Tooltip title={t('After downloading this commit, also go there')}>
            <VSCodeCheckbox
              checked={shouldGoto}
              onChange={e => {
                setShouldGoto((e.target as HTMLInputElement).checked);
              }}>
              <T>Go to</T>
            </VSCodeCheckbox>
          </Tooltip>
          <Tooltip
            title={t(
              'After downloading this commit, rebase it onto the public base of the current stack. Public commits will be copied instead of moved.',
            )}>
            <VSCodeCheckbox
              checked={rebaseType === 'rebase_base'}
              onChange={e => {
                setRebaseType((e.target as HTMLInputElement).checked ? 'rebase_base' : null);
              }}>
              <T>Rebase to Stack Base</T>
            </VSCodeCheckbox>
          </Tooltip>
          <Tooltip
            title={t(
              'After downloading this commit, rebase it on top of the current commit. Public commits will be copied instead of moved.',
            )}>
            <VSCodeCheckbox
              checked={rebaseType === 'rebase_ontop'}
              onChange={e => {
                setRebaseType((e.target as HTMLInputElement).checked ? 'rebase_ontop' : null);
              }}>
              <T>Rebase onto Stack</T>
            </VSCodeCheckbox>
          </Tooltip>
        </div>
      </div>
      {Internal.supportsCommitCloud && (
        <>
          <VSCodeDivider />
          <CommitCloudInfo />
        </>
      )}
    </DropdownFields>
  );
}
