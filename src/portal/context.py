from django.conf import settings

from .models import EventRole
from .seed import DEMO_ROLES


def portal(request):
    user = request.user
    return {
        "demo_mode": settings.DOGFOOD_SEED_SESSIONS,
        "demo_roles": [(key, label) for key, (label, _) in DEMO_ROLES.items()],
        "nav_is_judge": user.is_authenticated
        and EventRole.objects.filter(user=user, role=EventRole.Role.JUDGE).exists(),
    }
