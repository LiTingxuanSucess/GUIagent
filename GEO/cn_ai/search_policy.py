"""Per-site wire text; stored question identity always remains the original."""

SEARCH_INSTRUCTIONS = {
    'kimi': '请先联网搜索，再回答，并在回答里附上引用来源。',
    'deepseek': '请务必先联网搜索，根据搜索结果回答，并在回答里附上引用来源。不要在未搜索的情况下直接作答。',
    'doubao': '请务必先联网搜索，根据搜索结果回答，并在回答里附上引用来源。不要在未搜索的情况下直接作答。',
}


def outbound_prompt(site_id, prompt):
    suffix = SEARCH_INSTRUCTIONS.get(site_id)
    if not suffix or prompt.rstrip().endswith(suffix):
        return prompt
    return prompt + '\n\n' + suffix


def prompt_variants(site_id, prompt):
    # Legacy jobs already sent the original. Accept only exact known variants,
    # never a substring or an arbitrary instruction added by a different turn.
    return tuple(dict.fromkeys((prompt, outbound_prompt(site_id, prompt))))
