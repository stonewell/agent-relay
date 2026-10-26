"""Conservative write reservations (design section 8.3)."""

from .specs.task import TaskCore
from .verify import effective_scope


def _prefix(pattern: str) -> tuple[str, ...]:
    parts: list[str] = []
    for segment in pattern.replace("\\", "/").strip("/").lower().split("/"):
        if any(char in segment for char in "*?[:~") or segment.endswith((".", " ")):
            break
        parts.append(segment)
    return tuple(parts)


def patterns_overlap(left: str, right: str) -> bool:
    """Prove disjointness only through conflicting literal path segments.

    Literal file names also reserve their descendants to cover directory/file collisions.
    Unknown glob intersections, aliases and case variants are always overlaps.
    """
    a, b = _prefix(left), _prefix(right)
    return not any(x != y for x, y in zip(a, b, strict=False))


def writers_overlap(left: TaskCore, right: TaskCore) -> bool:
    if left.access == "read" or right.access == "read":
        return False
    a, b = effective_scope(left), effective_scope(right)
    if a is None or b is None:
        return True
    return any(patterns_overlap(x, y) for x in a for y in b)
