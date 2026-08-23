# Domain Model

This document defines the conceptual model for Archiver. It describes what the system means, independently of a particular implementation.

## 1. Content

**Content** is the sequence of bytes that makes up a regular file.

Content identity is determined by a cryptographic content hash. A pathname, filename, timestamp, inode, device identifier, or storage location is not content identity.

Two filesystem entries with the same content hash represent the same content even when:

- they have different names;
- they are in different directories;
- they are on different storage devices;
- one is a temporary working copy;
- their filesystem metadata differs.

A content hash identifies bytes, not a particular physical copy.

## 2. File instance

A **file instance** is an observed occurrence of content at a filesystem location.

A file instance includes facts such as:

- location;
- relative path;
- content identity;
- size;
- observed filesystem metadata.

The same content may have zero, one, or many file instances.

A rename changes the observed path of a file instance but does not change content identity when the bytes are unchanged.

## 3. Location

A **location** identifies a storage root that can contain file instances.

Examples include:

- a local directory;
- a mounted disk;
- a future managed archive store;
- a future temporary working area.

Paths recorded for files should be relative to a defined location whenever possible. This keeps content identity independent of machine-specific absolute paths.

## 4. Catalog

A **catalog** is a database describing known content, file instances, metadata, and related history.

A catalog can be useful without owning or managing any of the files it describes.

Cataloging is observational by default: reading a source to build or update a catalog does not imply authority to modify that source.

A catalog may describe duplicate content. Duplicate content is not automatically an error.

## 5. Archive

An **archive** is not a fundamentally different kind of database from a catalog.

Conceptually, an archive is a catalog associated with one or more **managed storage locations** for which Archiver has explicit responsibility.

The distinction is therefore primarily about authority and lifecycle:

- a catalog describes;
- an archive describes and manages designated storage.

Becoming an archive does not require replacing the catalog data model with an unrelated schema.

## 6. Scan

A **scan** observes a location and records the content found there.

A scan is a historical event. The current state of a location is derived from its most recent successful scan.

An incomplete or failed scan must not silently become the current state.

For the initial implementation, scanning is read-only with respect to source files.

## 7. Duplicate

Two file instances are **content duplicates** when they resolve to the same content identity.

Duplicate detection is therefore a query over content identity, not filename equality.

Files with the same name but different content are not content duplicates.

Files with different names but the same content are content duplicates.

Policy decisions about whether to keep, move, link, or remove duplicate physical copies belong to later operations and must not be implied by detection alone.

## 8. Metadata and tags

Metadata may come from multiple sources and at different times.

A **tag assertion** describes content identity, never an individual pathname. Applying a tag through a path first resolves the observed content, so every file instance of those bytes shares the assertion. Assertions remain catalog metadata when paths disappear.

Tag assertions are historical and provenance-aware rather than destructively overwritten values. Provenance distinguishes user and system assertions and identifies the producing source or tool, including its version and any stable method or configuration identifier needed to interpret the result. Results from different tools or tool versions must remain distinguishable.

Retraction changes whether an assertion is current without erasing its origin. When catalogs are merged, conflicting or repeated assertions should be preservable. An effective/current tag view may be derived by later policy, but historical assertions and provenance must not be silently discarded.

Tagging is outside Plan 001. Plan 003.2 introduces current tag operations and lightweight assertion/retraction persistence; full tag-history queries, merge policy, and conflict resolution remain later work.

## 9. Ingest

**Ingest** is an explicit operation from a source catalog/location toward a target archive.

An ingest must identify:

- the source;
- the target archive;
- applicable conflict/duplicate policy;
- what physical changes, if any, are authorized.

Ingest is distinct from scanning. Scanning observes. Ingest may eventually copy, move, reconcile, or otherwise mutate managed storage according to policy.

Ingest is outside Plan 001.

## 10. Working copy

A **working copy** is a temporary or cached physical copy created to perform expensive processing on faster or more convenient storage.

For example, large-scale tagging may operate on local SSD copies of files whose authoritative archive copy resides on slower storage.

A working copy:

- represents existing content;
- is not authoritative merely because it is local;
- can be discarded and recreated;
- must not silently become an archive copy;
- should retain enough association to reconnect results to the original content identity.

Working-copy management is outside Plan 001.

## 11. Authority

Archiver must distinguish between:

- observed storage, which it may read;
- managed archive storage, which it may be authorized to change;
- temporary working storage, which is derived and disposable.

Authority must be explicit. The existence of a catalog record does not by itself grant permission to modify a physical file.

## 12. Initial implementation boundary

Plan 001 establishes only the foundation needed to:

- create/open a catalog;
- identify file content by hash;
- scan a local directory without modifying it;
- persist scan observations;
- derive the current state from the latest successful scan;
- identify duplicate content.

Archive management, ingest, tags, merge policy, destructive duplicate resolution, and working-copy management come later.
