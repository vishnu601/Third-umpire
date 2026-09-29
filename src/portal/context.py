from django.conf import settings
from django.utils import timezone

from .models import Event, EventRole
from .seed import DEMO_ROLES


def portal(request):
    user = request.user
    return {
        "demo_mode": settings.DOGFOOD_SEED_SESSIONS,
        "demo_roles": [(key, label) for key, (label, _) in DEMO_ROLES.items()],
        # the community vote closing soonest, so voters can find the ballot from any page
        "open_vote": Event.objects.filter(voting_opens__lte=timezone.now(), voting_closes__gt=timezone.now())
        .order_by("voting_closes")
        .first(),
        "nav_is_judge": user.is_authenticated
        and EventRole.objects.filter(user=user, role=EventRole.Role.JUDGE).exists(),
    }
