# runmanager cleanup candidates

Simplifications in runmanager's remote submission and sequence code, found by
the reuse, simplification and altitude angles of a code review. None has been
verified one by one.

- Record the sequence attributes beside the last-sent anchor. The file read,
  its error wrappers, `get_sequence_attrs` and most of
  `get_sequence_attrs_to_extend` then become fallbacks for rows saved without
  attributes.
- `make_h5_files` keeps a `with_metadata=False` branch, a `sequence_globals`
  parameter and its own `get_sequence_attrs_to_extend` fallback that no
  production caller reaches.
- `compile_shots` keeps and coerces a caller-chosen `shot_id`, but no caller
  supplies one.
- The sequence record's next run number is always the stored path's index plus
  one. The remote join also passes `SUBMISSION_MODE_LAST_SEQUENCE` only for its
  anchor to be overwritten from the record.
- The anchor rules are explained in three places, and `n_runs` in three.
- The shot-id text rule is written twice (`compile_shots` and
  `_normalise_item`), and `get_shot_statuses` does not coerce, so id 7 and id
  '7' answer differently.
- `forget_last_sent` recomputes the anchor path that
  `get_last_sent_from_queue_filepath` computes.
- The tests re-declare stand-ins per file instead of in `tests/fixtures.py`:
  labconfigs, sequence dictionaries, `QueueManager` construction, and the
  `SubmittingApp` set-up.
- `handle_submit_shots` builds each entry's shot and frozen globals separately
  from `expand_pending_shots`, and the two already differ.
