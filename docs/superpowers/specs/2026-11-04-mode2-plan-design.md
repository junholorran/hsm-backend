# Mode 2: armed M15 plan lifecycle

User selected the tested candidate for today's rollout. Scope is armed primary M15 Ludo FVG plans only. Keep all capture, reaction qualification, structure, FVG geometry, entry, optional M5, protected-price and order-execution logic intact.

For an active armed primary native Ludo FVG plan, opposite M15 breaks and later origin-reaction changes produce a deduplicated PLANO_EM_ATENCAO warning; neither alone cancels/replaces the armed plan. Freeze original plan identity, episode, capture and protection until a real price/data invalidation. A new independent capture may create its own opposite plan; its alert must not cancel the older plan.

Keep strict M15 close beyond the FVG far edge, wick breach of the stored structural protection, missing/inconsistent data and origin/history/policy safety closures. Preserve native Ludo display removal rules. Unarmed, legacy and non-Ludo plans retain their old cancellation behavior. Never revive an invalidated plan, reset state/outbox or resend historical entries.

Warn once per new reaction episode and opposite M15 break; combine reasons within a bar; include original plan reference, zone, break time, current event and maintained-plan status. If a price invalidation happens on the same bar, send cancellation only. This is the selected operational candidate, not a universal ICT rule or a demonstrated trading edge.

Approaches compared: existing contextual cancellations versus the selected FVG/structural-price lifecycle. Prior SOL replay reproduced the same entry plans; only contextual cancellation differed. User authorized the latter prospectively. Four-pair replay and regression suite must pass before deployment; no exchange operations.
