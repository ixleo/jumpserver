from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from assets.const.web import FillType
from common.serializers import WritableNestedModelSerializer, type_field_map
from common.serializers.fields import LabeledChoiceField
from common.utils import lazyproperty
from ..const import Category, AllTypes
from ..models import Platform, PlatformProtocol, PlatformAutomation

__all__ = ["PlatformSerializer", "PlatformOpsMethodSerializer"]


class ProtocolSettingSerializer(serializers.Serializer):
    SECURITY_CHOICES = [
        ("any", "Any"),
        ("rdp", "RDP"),
        ("tls", "TLS"),
        ("nla", "NLA"),
    ]
    # RDP
    console = serializers.BooleanField(required=False, default=False)
    security = serializers.ChoiceField(choices=SECURITY_CHOICES, default="any")

    # SFTP
    sftp_enabled = serializers.BooleanField(default=True, label=_("SFTP enabled"))
    sftp_home = serializers.CharField(default="/tmp", label=_("SFTP home"))

    # HTTP
    autofill = serializers.ChoiceField(default='basic', choices=FillType.choices, label=_("Autofill"))
    username_selector = serializers.CharField(
        default="", allow_blank=True, label=_("Username selector")
    )
    password_selector = serializers.CharField(
        default="", allow_blank=True, label=_("Password selector")
    )
    submit_selector = serializers.CharField(
        default="", allow_blank=True, label=_("Submit selector")
    )
    script = serializers.JSONField(default=list, label=_("Script"))
    # Redis
    auth_username = serializers.BooleanField(default=False, label=_("Auth with username"))

    # WinRM
    use_ssl = serializers.BooleanField(default=False, label=_("Use SSL"))


class PlatformAutomationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformAutomation
        fields = [
            "id",
            "ansible_enabled", "ansible_config",
            "ping_enabled", "ping_method", "ping_params",
            "push_account_enabled", "push_account_method", "push_account_params",
            "gather_facts_enabled", "gather_facts_method", "gather_facts_params",
            "change_secret_enabled", "change_secret_method", "change_secret_params",
            "verify_account_enabled", "verify_account_method", "verify_account_params",
            "gather_accounts_enabled", "gather_accounts_method", "gather_accounts_params",
        ]
        extra_kwargs = {
            # 启用资产探测
            "ping_enabled": {"label": _("Ping enabled")},
            "ping_method": {"label": _("Ping method")},
            "gather_facts_enabled": {"label": _("Gather facts enabled")},
            "gather_facts_method": {"label": _("Gather facts method")},
            "verify_account_enabled": {"label": _("Verify account enabled")},
            "verify_account_method": {"label": _("Verify account method")},
            "change_secret_enabled": {"label": _("Change secret enabled")},
            "change_secret_method": {"label": _("Change secret method")},
            "push_account_enabled": {"label": _("Push account enabled")},
            "push_account_method": {"label": _("Push account method")},
            "gather_accounts_enabled": {"label": _("Gather accounts enabled")},
            "gather_accounts_method": {"label": _("Gather accounts method")},
        }


class PlatformProtocolSerializer(serializers.ModelSerializer):
    setting = ProtocolSettingSerializer(required=False, allow_null=True)

    class Meta:
        model = PlatformProtocol
        fields = [
            "id", "name", "port", "primary",
            "required", "default",
            "secret_types", "setting",
        ]


class PlatformCustomField(serializers.Serializer):
    TYPE_CHOICES = [(t, t) for t, c in type_field_map.items()]
    name = serializers.CharField(label=_("Name"), max_length=128)
    label = serializers.CharField(label=_("Label"), max_length=128)
    type = serializers.ChoiceField(choices=TYPE_CHOICES, label=_("Type"), default='str')
    default = serializers.CharField(default="", allow_blank=True, label=_("Default"), max_length=1024)
    help_text = serializers.CharField(default="", allow_blank=True, label=_("Help text"), max_length=1024)
    choices = serializers.ListField(default=list, label=_("Choices"), required=False)


class PlatformSerializer(WritableNestedModelSerializer):
    SU_METHOD_CHOICES = [
        ("sudo", "sudo su -"),
        ("su", "su - "),
        ("enable", "enable"),
        ("super", "super 15"),
        ("super_level", "super level 15")
    ]
    charset = LabeledChoiceField(choices=Platform.CharsetChoices.choices, label=_("Charset"), default='utf-8')
    type = LabeledChoiceField(choices=AllTypes.choices(), label=_("Type"))
    category = LabeledChoiceField(choices=Category.choices, label=_("Category"))
    protocols = PlatformProtocolSerializer(label=_("Protocols"), many=True, required=False)
    automation = PlatformAutomationSerializer(label=_("Automation"), required=False, default=dict)
    su_method = LabeledChoiceField(
        choices=SU_METHOD_CHOICES, label=_("Su method"),
        required=False, default="sudo", allow_null=True
    )
    custom_fields = PlatformCustomField(label=_("Custom fields"), many=True, required=False)

    class Meta:
        model = Platform
        fields_mini = ["id", "name", "internal"]
        fields_small = fields_mini + [
            "category", "type", "charset",
        ]
        read_only_fields = [
            'internal', 'date_created', 'date_updated',
            'created_by', 'updated_by'
        ]
        fields = fields_small + [
            "protocols", "domain_enabled", "su_enabled",
            "su_method", "automation", "comment", "custom_fields",
        ] + read_only_fields
        extra_kwargs = {
            "su_enabled": {"label": _('Su enabled')},
            "domain_enabled": {"label": _('Domain enabled')},
            "domain_default": {"label": _('Default Domain')},
        }

    @property
    def platform_category_type(self):
        if self.instance:
            return self.instance.category, self.instance.type
        if self.initial_data:
            return self.initial_data.get('category'), self.initial_data.get('type')
        raise serializers.ValidationError({'type': _("type is required")})

    def add_type_choices(self, name, label):
        tp = self.fields['type']
        tp.choices[name] = label
        tp.choice_mapper[name] = label
        tp.choice_strings_to_values[name] = label

    @lazyproperty
    def constraints(self):
        category, tp = self.platform_category_type
        constraints = AllTypes.get_constraints(category, tp)
        return constraints

    def validate(self, attrs):
        domain_enabled = attrs.get('domain_enabled', False) and self.constraints.get('domain_enabled', False)
        su_enabled = attrs.get('su_enabled', False) and self.constraints.get('su_enabled', False)
        automation = attrs.get('automation', {})
        automation['ansible_enabled'] = automation.get('ansible_enabled', False) \
                                        and self.constraints['automation'].get('ansible_enabled', False)
        attrs.update({
            'domain_enabled': domain_enabled,
            'su_enabled': su_enabled,
            'automation': automation,
        })
        self.initial_data['automation'] = automation
        return attrs

    @classmethod
    def setup_eager_loading(cls, queryset):
        queryset = queryset.prefetch_related(
            'protocols', 'automation'
        )
        return queryset

    def validate_protocols(self, protocols):
        if not protocols:
            raise serializers.ValidationError(_("Protocols is required"))
        primary = [p for p in protocols if p.get('primary')]
        if not primary:
            protocols[0]['primary'] = True
        # 这里不设置不行，write_nested 不使用 validated 中的
        self.initial_data['protocols'] = protocols
        return protocols


class PlatformOpsMethodSerializer(serializers.Serializer):
    id = serializers.CharField(read_only=True)
    name = serializers.CharField(max_length=50, label=_("Name"))
    category = serializers.CharField(max_length=50, label=_("Category"))
    type = serializers.ListSerializer(child=serializers.CharField())
    method = serializers.CharField()
