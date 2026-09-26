"""值守现场角色：值守工可建脂检，主管可作废。"""
from django.contrib.auth.models import Group

WORKER_GROUP = "值守工"
SUPERVISOR_GROUP = "主管"

_ROLE_GROUPS = (WORKER_GROUP, SUPERVISOR_GROUP)


def ensure_role_groups():
    """幂等建组。"""
    return [Group.objects.get_or_create(name=name)[0] for name in _ROLE_GROUPS]


def _group_names(user):
    if not getattr(user, "is_authenticated", False):
        return frozenset()
    return set(user.groups.values_list("name", flat=True))


def can_record_assay(user):
    """值守工可建脂检（主管、超管同样可建）。"""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    names = _group_names(user)
    return WORKER_GROUP in names or SUPERVISOR_GROUP in names


def can_void_assay(user):
    """仅主管可作废脂检（超管视作主管）。"""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return SUPERVISOR_GROUP in _group_names(user)
