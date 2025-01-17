// @generated SignedSource<<2949c9708f48e03ac8b2783ac5d9667d>>
// DO NOT EDIT THIS FILE MANUALLY!
// This file is a mechanical copy of the version in the configerator repo. To
// modify it, edit the copy in the configerator repo instead and copy it over by
// running the following in your fbcode directory:
//
// configerator-thrift-updater scm/mononoke/tunables/tunables.thrift

/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

namespace py configerator.mononoke.tunables

typedef map<string, bool> (rust.type = "HashMap") TunableBools
typedef map<string, i64> (rust.type = "HashMap") TunableInts
typedef map<string, string> (rust.type = "HashMap") TunableStrings
typedef map<string, list<string>> (rust.type = "HashMap") TunableStringLists

struct Tunables {
  1: TunableBools killswitches;
  2: TunableInts ints;
  3: TunableStrings strings;
  8: TunableStringLists vec_of_strings;

  4: optional map<string, TunableBools> (
    rust.type = "HashMap",
  ) killswitches_by_repo;
  5: optional map<string, TunableInts> (rust.type = "HashMap") ints_by_repo;
  6: optional map<string, TunableStrings> (
    rust.type = "HashMap",
  ) strings_by_repo;
  7: optional map<string, TunableStringLists> (
    rust.type = "HashMap",
  ) vec_of_strings_by_repo;
} (rust.exhaustive)
