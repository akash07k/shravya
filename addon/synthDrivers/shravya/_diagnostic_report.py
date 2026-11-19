"""Format an in-memory latency capture for NVDA's browsable message."""

from __future__ import annotations

from statistics import median
from typing import TYPE_CHECKING

from ._diagnostic_measurements import TimingEvent, Workload, model_loads_ns, summarize

if TYPE_CHECKING:
	from gettext import gettext as _


def _timings(label: str, values: list[int]) -> str:
	if not values:
		# Translators: A diagnostic timing has no usable samples.
		return _("{label}: No valid samples").format(label=label)
	# Translators: Summary statistics for a timing measured in milliseconds.
	return _(
		"{label}: median {median:.3f}, minimum {minimum:.3f}, maximum {maximum:.3f} milliseconds"
	).format(
		label=label,
		median=median(values) / 1_000_000,
		minimum=min(values) / 1_000_000,
		maximum=max(values) / 1_000_000,
	)


def format_report(events: list[TimingEvent]) -> str:
	results = summarize(events)
	# Translators: Explain the scope and limitations of the latency report.
	lines = [
		_("Captured keyboard event to detected Windows output onset; all timings are in milliseconds."),
		_("These are temporal observations, not proof of cause."),
		_("Other applications can affect output, and physical speaker or headphone delay is not included."),
	]
	# Translators: Names of the two latency diagnostic workloads.
	workloads: tuple[tuple[Workload, str], ...] = (
		("controlled", _("Controlled test")),
		("live", _("Live navigation and typing")),
	)
	for kind, title in workloads:
		result = results[kind]
		lines.extend(
			[
				"",
				title,
				_("Valid: {count}").format(count=len(result.latencies_ns)),
				_("Interrupted: {count}").format(count=result.interrupted),
				_("Ambiguous: {count}").format(count=result.ambiguous),
				_timings(_("Key to output"), result.latencies_ns),
				_timings(_("Queue"), result.queue_ns),
				_timings(_("Synthesis to first playable audio"), result.synthesis_ns),
			]
		)
	loads = model_loads_ns(events)
	lines.append("")
	if loads:
		lines.append(_timings(_("Observed model load"), loads))
	else:
		# Translators: No complete model load was observed during capture.
		lines.append(_("Model load: not observed during capture"))
	return "\n".join(lines)
