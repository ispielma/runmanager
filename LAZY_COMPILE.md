# Lazy and eager compile in the shot queue

The queue tab's *Compile mode* is *Eager compile* or *Lazy compile*. It applies
to the shots engaged after it is set and is kept on each shot's row, so one queue
can hold both.

## Engage

Engage expands the globals into shots. Each gets a file name and a run number and
becomes a record, but Engage writes no shot file. A record holds:

- the frozen globals: every global not marked JIT, a scanned one at that shot's
  own value and the others as their default expressions;
- the compile mode;
- whether *runviewer* was ticked, which is that shot's own choice.

With the BLACS checkbox ticked the records join the queue at once, as rows. With
it unticked they are not queued: the batch is compiled in order, for runviewer if
that is ticked, and *Empty queue* stops it after the shot it is on.

## Compiling

`QueueManager.compile_shot` compiles every shot, whatever asks for it. It writes
the shot file from the row's frozen globals and the JIT globals as they are then
(a default shot, from the globals' defaults), compiles it in the compiler
subprocess, and sends it to runviewer if the row says so.

- Eager rows are compiled in queue order by the queue's worker as soon as they
  are queued, ahead of BLACS asking.
- Lazy rows are compiled when BLACS asks for a shot and the row is at the head,
  off the request thread. Until it is done runmanager answers BLACS
  `PROVIDER_PENDING`, so BLACS waits instead of running its local override. An
  uncompiled row at the head is compiled this way whatever its mode.

Only a compiled row is offered to BLACS. The queue is saved with the
configuration and restored at startup, when eager rows that were not compiled are
compiled ahead.

## When a compile fails

The row stays where it is and goes red; its tooltip and the output box say why.
BLACS is offered nothing while it is at the head, and the rows behind it wait.
Nothing retries it, since a shot that always fails would be recompiled for ever.
Right-click the row and choose *Compile again*, or delete it. Every compile
rewrites the shot file from scratch, so a row that failed once can succeed.

Restarting the compiler subprocess fails the compile in flight, and later
compiles wait until the new subprocess is up.
