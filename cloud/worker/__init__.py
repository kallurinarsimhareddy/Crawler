"""The side of the control plane that does the work.

Phase 5A has the seams and a fake: :class:`~cloud.worker.runner.JobRunner` is
what a real CareerCrawler runner will implement, and
:class:`~cloud.worker.executor.JobExecutor` is the loop body a queue consumer
will call. Nothing here imports the crawler yet.
"""
