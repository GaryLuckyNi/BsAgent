def echo(text: str) -> dict:
    return {"text": text}


def calculate_sum(numbers: list[float]) -> dict:
    total = sum(numbers)
    normalized_total = int(total) if total == int(total) else total
    return {"total": normalized_total, "count": len(numbers)}
