/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use rate_limiting::RateLimitReason;
use thiserror::Error;

use mercurial_types::{HgNodeHash, RepoPath};

#[derive(Debug, Error)]
pub enum ErrorKind {
    #[error("Data corruption for {path}: expected {expected}, actual {actual}!")]
    DataCorruption {
        path: RepoPath,
        expected: HgNodeHash,
        actual: HgNodeHash,
    },
    #[error("Request {request_name} was throttled")]
    RequestThrottled {
        request_name: String,
        #[source]
        reason: RateLimitReason,
    },
}
