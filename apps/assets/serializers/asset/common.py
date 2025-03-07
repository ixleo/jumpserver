# -*- coding: utf-8 -*-
#

from django.db.models import F
from django.db.transaction import atomic
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from accounts.models import Account
from accounts.serializers import AccountSerializer
from common.const import UUID_PATTERN
from common.serializers import (
    WritableNestedModelSerializer, SecretReadableMixin,
    CommonModelSerializer, MethodSerializer
)
from common.serializers.common import DictSerializer
from common.serializers.fields import LabeledChoiceField
from orgs.mixins.serializers import BulkOrgResourceModelSerializer
from ...const import Category, AllTypes
from ...models import Asset, Node, Platform, Label, Protocol

__all__ = [
    'AssetSerializer', 'AssetSimpleSerializer', 'MiniAssetSerializer',
    'AssetTaskSerializer', 'AssetsTaskSerializer', 'AssetProtocolsSerializer',
    'AssetDetailSerializer', 'DetailMixin', 'AssetAccountSerializer',
    'AccountSecretSerializer', 'AssetProtocolsPermsSerializer'
]


class AssetProtocolsSerializer(serializers.ModelSerializer):
    port = serializers.IntegerField(required=False, allow_null=True, max_value=65535, min_value=1)

    def to_file_representation(self, data):
        return '{name}/{port}'.format(**data)

    def to_file_internal_value(self, data):
        name, port = data.split('/')
        return {'name': name, 'port': port}

    class Meta:
        model = Protocol
        fields = ['name', 'port']


class AssetProtocolsPermsSerializer(AssetProtocolsSerializer):
    class Meta(AssetProtocolsSerializer.Meta):
        fields = AssetProtocolsSerializer.Meta.fields + ['public', 'setting']


class AssetLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = ['id', 'name', 'value']
        extra_kwargs = {
            # 取消默认唯一键的校验
            'id': {'validators': []},
            'name': {'required': False},
            'value': {'required': False},
        }


class AssetPlatformSerializer(serializers.ModelSerializer):
    class Meta:
        model = Platform
        fields = ['id', 'name']
        extra_kwargs = {
            'name': {'required': False}
        }


class AssetAccountSerializer(AccountSerializer):
    add_org_fields = False
    asset = serializers.PrimaryKeyRelatedField(queryset=Asset.objects, required=False, write_only=True)
    clone_id = None

    def to_internal_value(self, data):
        # 导入时，data有时为str
        if isinstance(data, str):
            return super().to_internal_value(data)

        clone_id = data.pop('id', None)
        ret = super().to_internal_value(data)
        self.clone_id = clone_id
        return ret

    def set_secret(self, attrs):
        _id = self.clone_id
        if not _id:
            return attrs

        account = Account.objects.get(id=_id)
        attrs['secret'] = account.secret
        return attrs

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return self.set_secret(attrs)

    class Meta(AccountSerializer.Meta):
        fields = [
            f for f in AccountSerializer.Meta.fields
            if f not in ['spec_info']
        ]
        extra_kwargs = {
            **AccountSerializer.Meta.extra_kwargs,
        }


class AccountSecretSerializer(SecretReadableMixin, CommonModelSerializer):
    class Meta:
        model = Account
        fields = [
            'name', 'username', 'privileged', 'secret_type', 'secret',
        ]
        extra_kwargs = {
            'secret': {'write_only': False},
        }


class AssetSerializer(BulkOrgResourceModelSerializer, WritableNestedModelSerializer):
    category = LabeledChoiceField(choices=Category.choices, read_only=True, label=_('Category'))
    type = LabeledChoiceField(choices=AllTypes.choices(), read_only=True, label=_('Type'))
    labels = AssetLabelSerializer(many=True, required=False, label=_('Label'))
    protocols = AssetProtocolsSerializer(many=True, required=False, label=_('Protocols'), default=())
    accounts = AssetAccountSerializer(many=True, required=False, allow_null=True, write_only=True, label=_('Account'))
    nodes_display = serializers.ListField(read_only=False, required=False, label=_("Node path"))

    class Meta:
        model = Asset
        fields_mini = ['id', 'name', 'address']
        fields_small = fields_mini + ['is_active', 'comment']
        fields_fk = ['domain', 'platform']
        fields_m2m = [
            'nodes', 'labels', 'protocols',
            'nodes_display', 'accounts',
        ]
        read_only_fields = [
            'category', 'type', 'connectivity', 'auto_config',
            'date_verified', 'created_by', 'date_created',
        ]
        fields = fields_small + fields_fk + fields_m2m + read_only_fields
        fields_unexport = ['auto_config']
        extra_kwargs = {
            'auto_config': {'label': _('Auto info')},
            'name': {'label': _("Name")},
            'address': {'label': _('Address')},
            'nodes_display': {'label': _('Node path')},
            'nodes': {'allow_empty': True},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_field_choices()

    def _get_protocols_required_default(self):
        platform = self._asset_platform
        platform_protocols = platform.protocols.all()
        protocols_default = [p for p in platform_protocols if p.default]
        protocols_required = [p for p in platform_protocols if p.required or p.primary]
        return protocols_required, protocols_default

    def _set_protocols_default(self):
        if not hasattr(self, 'initial_data'):
            return
        protocols = self.initial_data.get('protocols')
        if protocols is not None:
            return

        protocols_required, protocols_default = self._get_protocols_required_default()
        protocols_data = [
            {'name': p.name, 'port': p.port}
            for p in protocols_required + protocols_default
        ]
        self.initial_data['protocols'] = protocols_data

    def _init_field_choices(self):
        request = self.context.get('request')
        if not request:
            return
        category = request.path.strip('/').split('/')[-1].rstrip('s')
        field_category = self.fields.get('category')
        if not field_category:
            return
        field_category.choices = Category.filter_choices(category)
        field_type = self.fields.get('type')
        if not field_type:
            return
        field_type.choices = AllTypes.filter_choices(category)

    @classmethod
    def setup_eager_loading(cls, queryset):
        """ Perform necessary eager loading of data. """
        queryset = queryset.prefetch_related('domain', 'nodes', 'labels', 'protocols') \
            .prefetch_related('platform', 'platform__automation') \
            .annotate(category=F("platform__category")) \
            .annotate(type=F("platform__type"))
        return queryset

    @staticmethod
    def perform_nodes_display_create(instance, nodes_display):
        if not nodes_display:
            return
        nodes_to_set = []
        for full_value in nodes_display:
            if not full_value.startswith('/'):
                full_value = '/' + instance.org.name + '/' + full_value
            node = Node.objects.filter(full_value=full_value).first()
            if node:
                nodes_to_set.append(node)
            else:
                node = Node.create_node_by_full_value(full_value)
            nodes_to_set.append(node)
        instance.nodes.set(nodes_to_set)

    @property
    def _asset_platform(self):
        platform_id = self.initial_data.get('platform')
        if isinstance(platform_id, dict):
            platform_id = platform_id.get('id') or platform_id.get('pk')

        if not platform_id and self.instance:
            platform = self.instance.platform
        else:
            platform = Platform.objects.filter(id=platform_id).first()

        if not platform:
            raise serializers.ValidationError({'platform': _("Platform not exist")})
        return platform

    def validate_domain(self, value):
        platform = self._asset_platform
        if platform.domain_enabled:
            return value
        else:
            return None

    def validate_nodes(self, nodes):
        if nodes:
            return nodes
        nodes_display = self.initial_data.get('nodes_display')
        if nodes_display:
            return nodes
        default_node = Node.org_root()
        request = self.context.get('request')
        if not request:
            return [default_node]
        node_id = request.query_params.get('node_id')
        if not node_id:
            return [default_node]
        nodes = Node.objects.filter(id=node_id)
        return nodes

    def is_valid(self, raise_exception=False):
        self._set_protocols_default()
        return super().is_valid(raise_exception)

    def validate_protocols(self, protocols_data):
        # 目的是去重
        protocols_data_map = {p['name']: p for p in protocols_data}
        for p in protocols_data:
            port = p.get('port', 0)
            if port < 1 or port > 65535:
                error = p.get('name') + ': ' + _("port out of range (1-65535)")
                raise serializers.ValidationError(error)

        protocols_required, protocols_default = self._get_protocols_required_default()
        protocols_not_found = [p.name for p in protocols_required if p.name not in protocols_data_map]
        if protocols_not_found:
            raise serializers.ValidationError({
                'protocols': _("Protocol is required: {}").format(', '.join(protocols_not_found))
            })
        return protocols_data_map.values()

    @staticmethod
    def accounts_create(accounts_data, asset):
        if not accounts_data:
            return
        for data in accounts_data:
            data['asset'] = asset.id

        s = AssetAccountSerializer(data=accounts_data, many=True)
        s.is_valid(raise_exception=True)
        s.save()

    @atomic
    def create(self, validated_data):
        nodes_display = validated_data.pop('nodes_display', '')
        accounts = validated_data.pop('accounts', [])
        instance = super().create(validated_data)
        self.accounts_create(accounts, instance)
        self.perform_nodes_display_create(instance, nodes_display)
        return instance

    @atomic
    def update(self, instance, validated_data):
        if not validated_data.get('accounts'):
            validated_data.pop('accounts', None)
        nodes_display = validated_data.pop('nodes_display', '')
        instance = super().update(instance, validated_data)
        self.perform_nodes_display_create(instance, nodes_display)
        return instance


class DetailMixin(serializers.Serializer):
    accounts = AssetAccountSerializer(many=True, required=False, label=_('Accounts'))
    spec_info = MethodSerializer(label=_('Spec info'), read_only=True)
    gathered_info = MethodSerializer(label=_('Gathered info'), read_only=True)
    auto_config = serializers.DictField(read_only=True, label=_('Auto info'))

    def get_instance(self):
        request = self.context.get('request')
        if not self.instance and UUID_PATTERN.findall(request.path):
            pk = UUID_PATTERN.findall(request.path)[0]
            self.instance = Asset.objects.filter(id=pk).first()
        return self.instance

    def get_field_names(self, declared_fields, info):
        names = super().get_field_names(declared_fields, info)
        names.extend([
            'accounts', 'gathered_info', 'spec_info',
            'auto_config',
        ])
        return names

    def get_category(self):
        request = self.context.get('request')
        if request.query_params.get('category'):
            category = request.query_params.get('category')
        else:
            instance = self.get_instance()
            category = instance.category if instance else 'host'
        return category

    def get_gathered_info_serializer(self):
        category = self.get_category()
        from .info.gathered import category_gathered_serializer_map
        serializer_cls = category_gathered_serializer_map.get(category, DictSerializer)
        return serializer_cls()

    def get_spec_info_serializer(self):
        category = self.get_category()
        from .info.spec import category_spec_serializer_map
        serializer_cls = category_spec_serializer_map.get(category, DictSerializer)
        return serializer_cls()


class AssetDetailSerializer(DetailMixin, AssetSerializer):
    pass


class MiniAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = AssetSerializer.Meta.fields_mini


class AssetSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = [
            'id', 'name', 'address', 'port',
            'connectivity', 'date_verified'
        ]


class AssetsTaskSerializer(serializers.Serializer):
    ACTION_CHOICES = (
        ('refresh', 'refresh'),
        ('test', 'test'),
    )
    task = serializers.CharField(read_only=True)
    action = serializers.ChoiceField(choices=ACTION_CHOICES, write_only=True)
    assets = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, required=False, allow_empty=True, many=True
    )


class AssetTaskSerializer(AssetsTaskSerializer):
    ACTION_CHOICES = tuple(list(AssetsTaskSerializer.ACTION_CHOICES) + [
        ('push_system_user', 'push_system_user'),
        ('test_system_user', 'test_system_user')
    ])
    action = serializers.ChoiceField(choices=ACTION_CHOICES, write_only=True)
    asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects, required=False, allow_empty=True, many=False
    )
    accounts = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects, required=False, allow_empty=True, many=True
    )
