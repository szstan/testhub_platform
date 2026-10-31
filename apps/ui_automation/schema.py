from rest_framework import serializers


class UiDashboardAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class UiDashboardStatsResponseSerializer(serializers.Serializer):
    project_count = serializers.IntegerField()
    test_case_count = serializers.IntegerField()
    suite_count = serializers.IntegerField()
    execution_count = serializers.IntegerField()


class UiEnvironmentAuthErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class UiSystemBrowserStatusSerializer(serializers.Serializer):
    name = serializers.ChoiceField(choices=('chrome', 'firefox', 'edge', 'safari'))
    installed = serializers.BooleanField()
    version = serializers.CharField(allow_null=True)
    install_cmd = serializers.CharField()


class UiPlaywrightBrowserStatusSerializer(serializers.Serializer):
    name = serializers.ChoiceField(choices=('chromium', 'firefox', 'webkit'))
    installed = serializers.BooleanField()
    version = serializers.CharField(allow_null=True)
    install_cmd = serializers.CharField()


class UiEnvironmentCheckResponseSerializer(serializers.Serializer):
    os = serializers.CharField()
    system_browsers = UiSystemBrowserStatusSerializer(many=True)
    playwright_browsers = UiPlaywrightBrowserStatusSerializer(many=True)


class UiEnvironmentInstallDriverRequestSerializer(serializers.Serializer):
    browser = serializers.ChoiceField(choices=('chromium', 'firefox', 'webkit'))


class UiEnvironmentInstallDriverSuccessResponseSerializer(serializers.Serializer):
    message = serializers.CharField()


class UiEnvironmentErrorResponseSerializer(serializers.Serializer):
    error = serializers.CharField()
