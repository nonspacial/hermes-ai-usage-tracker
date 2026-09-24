"""Opt-in ledger response shape. Absent view keeps the legacy full contract."""

GROUP_FIELDS = {
    'model': 'model_groups', 'time': None, 'project': 'project_groups',
    'session': 'session_groups', 'subagent': 'subagent_groups',
}
VIEW_FIELDS = {
    'overview': frozenset(),
    'requests': frozenset(('requests', 'cache_read_progression')),
    'cache': frozenset(('price_catalogs', 'applied_rate_groups')),
    'compressions': frozenset(('compressions', 'compression_truncated')),
    'models': frozenset(('groups',)),
    'skills': frozenset(),  # Skills history has its own route; the ledger supplies the header.
}
COMMON = frozenset(('generated_at', 'seq', 'window', 'summary', 'request_count', 'next_offset',
    'provider_groups', 'trend', 'subagent_summary', 'project_options', 'providers',
    'compression_count', 'tests'))
ALL_FIELDS = COMMON | frozenset(('requests', 'cache_read_progression', 'price_catalogs',
    'applied_rate_groups', 'compressions', 'compression_truncated', 'groups',
    'model_groups', 'project_groups', 'session_groups', 'subagent_groups',
    'agent_groups', 'health', 'rates', 'quota_observations', 'crossing_start', 'crossing_end'))


def selected_fields(view=None, group=None):
    """Return None for the exact legacy response, otherwise a validated field set."""
    if view is None:
        if group is not None:
            raise ValueError('Group requires a view.')
        return None
    if view not in VIEW_FIELDS:
        raise ValueError('Invalid ledger view.')
    if view == 'overview':
        if group not in GROUP_FIELDS:
            raise ValueError('Overview requires a valid group.')
        breakdown = GROUP_FIELDS[group]
    elif group is not None:
        raise ValueError('Group applies only to Overview.')
    else:
        breakdown = None
    fields = COMMON | VIEW_FIELDS[view]
    if breakdown:
        fields |= {breakdown}
    return fields


def manifest(fields):
    return {'version': 1, 'included': sorted(fields), 'omitted': sorted(ALL_FIELDS - fields)}
