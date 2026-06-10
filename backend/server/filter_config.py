from __future__ import annotations

from storage import COLLECTION_CONFIG_DIR, COMMON_FILTERS_PATH, APPLICATIONS_DIR, load_json


def resolve_filter_config(application_id: str, selected_collection_ids: list[str]):
    common = load_json(COMMON_FILTERS_PATH).get('filters', {})
    app = load_json(APPLICATIONS_DIR / f'{application_id}.json')
    collection_cfgs = [load_json(COLLECTION_CONFIG_DIR / f'{collection_id}.json') for collection_id in selected_collection_ids]

    include = app['filters']['include']
    ordering = app['filters']['ordering']
    groups = app['filters']['groups']

    supported_union = {}
    for cfg in collection_cfgs:
        for filter_id, filter_cfg in cfg.get('supported_filters', {}).items():
            supported_union.setdefault(filter_id, {'configs': [], 'collection_ids': []})
            supported_union[filter_id]['configs'].append(filter_cfg)
            supported_union[filter_id]['collection_ids'].append(cfg['collection_id'])

    resolved_filters = {}
    for filter_id in ordering:
        if filter_id not in include or filter_id not in supported_union:
            continue
        support = supported_union[filter_id]
        base = common.get(filter_id, {})
        first_cfg = support['configs'][0]
        values_ref = first_cfg.get('values_ref')
        values_path = (COLLECTION_CONFIG_DIR / values_ref).resolve() if values_ref else None
        values = load_json(values_path).get('values', []) if values_path else []
        resolved_filters[filter_id] = {
            'filter_id': filter_id,
            'label': first_cfg.get('label') or base.get('label') or filter_id.title(),
            'kind': first_cfg.get('kind') or base.get('kind') or 'enum',
            'selection': first_cfg.get('selection') or base.get('selection') or 'multi',
            'description': first_cfg.get('description') or base.get('description'),
            'values': values,
            'coverage': 'all' if len(support['collection_ids']) == len(selected_collection_ids) else 'partial',
            'applies_to_collection_ids': support['collection_ids']
        }

    resolved_groups = []
    for group_id, group_cfg in groups.items():
        filters = [resolved_filters[filter_id] for filter_id in group_cfg.get('filters', []) if filter_id in resolved_filters]
        if filters:
            resolved_groups.append({'group_id': group_id, 'label': group_cfg['label'], 'filters': filters})

    return {
        'application_id': application_id,
        'selected_collection_ids': selected_collection_ids,
        'groups': resolved_groups
    }
