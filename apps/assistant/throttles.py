from django.conf import settings
from rest_framework.throttling import UserRateThrottle


class AssistantMessageRateThrottle(UserRateThrottle):
    scope = 'assistant_message'
    rate = '30/minute'

    def __init__(self):
        self.rate = getattr(settings, 'ASSISTANT_MESSAGE_RATE', self.rate)
        super().__init__()
