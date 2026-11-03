"""Windows plan admission, independent of current on-disk metadata."""
from .paths import PLATFORM_ID, ARCHIVE_UNAVAILABLE, absolute, component, unsupported_name


def check_plan(plan):
    from ..planner import PlanError
    if plan.get('platform') != PLATFORM_ID:
        raise PlanError('Windows 只接受本平台生成并确认的计划。')
    if (plan.get('schema_version') != 3 or plan.get('archive', {}).get('enabled') is not False or
            plan.get('directory_operations') != [] or plan.get('plan_type') == 'remaining_archive'):
        raise PlanError(ARCHIVE_UNAVAILABLE)
    try:
        for key in ('downloads_dir', 'target_root'):
            if absolute(plan[key]) != plan[key]:
                raise ValueError('计划路径不是规范形式。')
        for op in plan['operations']:
            for key in ('source', 'dest'):
                if absolute(op[key]) != op[key]:
                    raise ValueError('计划路径不是规范形式。')
            parts = op['rel_path'].split('/')
            for part in parts:
                component(part)
            if unsupported_name(parts[-1]):
                raise ValueError('计划包含不支持的名称或快捷方式。')
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise PlanError('Windows 计划路径不符合安全限制，请重新预览。') from exc
