import uuid

from django.db import IntegrityError
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator

from accounts.const import SecretType, Source, AccountInvalidPolicy
from accounts.models import Account, AccountTemplate
from accounts.tasks import push_accounts_to_assets_task
from assets.const import Category, AllTypes
from assets.models import Asset
from common.serializers import SecretReadableMixin
from common.serializers.fields import ObjectRelatedField, LabeledChoiceField
from common.utils import get_logger
from .base import BaseAccountSerializer, AuthValidateMixin

logger = get_logger(__name__)


class AccountCreateUpdateSerializerMixin(serializers.Serializer):
    template = serializers.PrimaryKeyRelatedField(
        queryset=AccountTemplate.objects,
        required=False, label=_("Template"), write_only=True
    )
    push_now = serializers.BooleanField(
        default=False, label=_("Push now"), write_only=True
    )
    params = serializers.JSONField(
        decoder=None, encoder=None, required=False, style={'base_template': 'textarea.html'}
    )
    on_invalid = LabeledChoiceField(
        choices=AccountInvalidPolicy.choices, default=AccountInvalidPolicy.ERROR,
        write_only=True, label=_('Exist policy')
    )
    _template = None

    class Meta:
        fields = ['template', 'push_now', 'params', 'on_invalid']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_initial_value()

    def set_initial_value(self):
        if not getattr(self, 'initial_data', None):
            return
        if isinstance(self.initial_data, dict):
            initial_data = [self.initial_data]
        else:
            initial_data = self.initial_data

        for data in initial_data:
            if not data.get('asset') and not self.instance:
                raise serializers.ValidationError({'asset': UniqueTogetherValidator.missing_message})
            asset = data.get('asset') or self.instance.asset
            self.from_template_if_need(data)
            self.set_uniq_name_if_need(data, asset)

    def to_internal_value(self, data):
        self.from_template_if_need(data)
        return super().to_internal_value(data)

    def set_uniq_name_if_need(self, initial_data, asset):
        name = initial_data.get('name')
        if name is not None:
            return
        if not name:
            name = initial_data.get('username')
        if self.instance and self.instance.name == name:
            return
        if Account.objects.filter(name=name, asset=asset).exists():
            name = name + '_' + uuid.uuid4().hex[:4]
        initial_data['name'] = name

    def from_template_if_need(self, initial_data):
        if isinstance(initial_data, str):
            return

        template_id = initial_data.pop('template', None)
        if not template_id:
            return

        if isinstance(template_id, (str, uuid.UUID)):
            template = AccountTemplate.objects.filter(id=template_id).first()
        else:
            template = template_id
        if not template:
            raise serializers.ValidationError({'template': 'Template not found'})

        self._template = template
        # Set initial data from template
        ignore_fields = ['id', 'date_created', 'date_updated', 'org_id']
        field_names = [
            field.name for field in template._meta.fields
            if field.name not in ignore_fields
        ]
        attrs = {}
        for name in field_names:
            value = getattr(template, name, None)
            if value is None:
                continue
            attrs[name] = value
        initial_data.update(attrs)

    @staticmethod
    def push_account_if_need(instance, push_now, params, stat):
        if not push_now or stat not in ['created', 'updated']:
            return
        push_accounts_to_assets_task.delay([str(instance.id)], params)

    def get_validators(self):
        _validators = super().get_validators()
        if getattr(self, 'initial_data', None) is None:
            return _validators

        on_invalid = self.initial_data.get('on_invalid')
        if on_invalid == AccountInvalidPolicy.ERROR and not self.parent:
            return _validators
        _validators = [v for v in _validators if not isinstance(v, UniqueTogetherValidator)]
        return _validators

    @staticmethod
    def do_create(vd):
        on_invalid = vd.pop('on_invalid', None)

        q = Q()
        if vd.get('name'):
            q |= Q(name=vd['name'])
        if vd.get('username'):
            q |= Q(username=vd['username'], secret_type=vd.get('secret_type'))

        instance = Account.objects.filter(asset=vd['asset']).filter(q).first()
        # 不存在这个资产，不用关系策略
        if not instance:
            instance = Account.objects.create(**vd)
            return instance, 'created'

        if on_invalid == AccountInvalidPolicy.SKIP:
            return instance, 'skipped'
        elif on_invalid == AccountInvalidPolicy.UPDATE:
            for k, v in vd.items():
                setattr(instance, k, v)
            instance.save()
            return instance, 'updated'
        else:
            raise serializers.ValidationError('Account already exists')

    def generate_source_data(self, validated_data):
        template = self._template
        if template is None:
            return
        validated_data['source'] = Source.TEMPLATE
        validated_data['source_id'] = str(template.id)

    def create(self, validated_data):
        push_now = validated_data.pop('push_now', None)
        params = validated_data.pop('params', None)
        self.generate_source_data(validated_data)
        instance, stat = self.do_create(validated_data)
        self.push_account_if_need(instance, push_now, params, stat)
        return instance

    def update(self, instance, validated_data):
        # account cannot be modified
        validated_data.pop('username', None)
        validated_data.pop('on_invalid', None)
        push_now = validated_data.pop('push_now', None)
        params = validated_data.pop('params', None)
        validated_data['source_id'] = None
        instance = super().update(instance, validated_data)
        self.push_account_if_need(instance, push_now, params, 'updated')
        return instance


class AccountAssetSerializer(serializers.ModelSerializer):
    platform = ObjectRelatedField(read_only=True)
    category = LabeledChoiceField(choices=Category.choices, read_only=True, label=_('Category'))
    type = LabeledChoiceField(choices=AllTypes.choices(), read_only=True, label=_('Type'))

    class Meta:
        model = Asset
        fields = ['id', 'name', 'address', 'type', 'category', 'platform', 'auto_config']

    def to_internal_value(self, data):
        if isinstance(data, dict):
            i = data.get('id') or data.get('pk')
        else:
            i = data

        try:
            return Asset.objects.get(id=i)
        except Asset.DoesNotExist:
            raise serializers.ValidationError(_('Asset not found'))


class AccountSerializer(AccountCreateUpdateSerializerMixin, BaseAccountSerializer):
    asset = AccountAssetSerializer(label=_('Asset'))
    source = LabeledChoiceField(choices=Source.choices, label=_("Source"), read_only=True)
    has_secret = serializers.BooleanField(label=_("Has secret"), read_only=True)
    su_from = ObjectRelatedField(
        required=False, queryset=Account.objects, allow_null=True, allow_empty=True,
        label=_('Su from'), attrs=('id', 'name', 'username')
    )

    class Meta(BaseAccountSerializer.Meta):
        model = Account
        fields = BaseAccountSerializer.Meta.fields + [
            'su_from', 'asset', 'version',
            'source', 'source_id', 'connectivity',
        ] + AccountCreateUpdateSerializerMixin.Meta.fields
        read_only_fields = BaseAccountSerializer.Meta.read_only_fields + [
            'source', 'source_id', 'connectivity'
        ]
        extra_kwargs = {
            **BaseAccountSerializer.Meta.extra_kwargs,
            'name': {'required': False},
        }

    @classmethod
    def setup_eager_loading(cls, queryset):
        """ Perform necessary eager loading of data. """
        queryset = queryset.prefetch_related(
            'asset', 'asset__platform',
            'asset__platform__automation'
        )
        return queryset


class AssetAccountBulkSerializerResultSerializer(serializers.Serializer):
    asset = serializers.CharField(read_only=True, label=_('Asset'))
    state = serializers.CharField(read_only=True, label=_('State'))
    error = serializers.CharField(read_only=True, label=_('Error'))
    changed = serializers.BooleanField(read_only=True, label=_('Changed'))


class AssetAccountBulkSerializer(
    AccountCreateUpdateSerializerMixin, AuthValidateMixin, serializers.ModelSerializer
):
    assets = serializers.PrimaryKeyRelatedField(queryset=Asset.objects, many=True, label=_('Assets'))

    class Meta:
        model = Account
        fields = [
            'name', 'username', 'secret', 'secret_type',
            'privileged', 'is_active', 'comment', 'template',
            'on_invalid', 'push_now', 'assets',
        ]
        extra_kwargs = {
            'name': {'required': False},
            'secret_type': {'required': False},
        }

    def set_initial_value(self):
        if not getattr(self, 'initial_data', None):
            return
        initial_data = self.initial_data
        self.from_template_if_need(initial_data)

    @staticmethod
    def get_filter_lookup(vd):
        return {
            'username': vd['username'],
            'secret_type': vd['secret_type'],
            'asset': vd['asset'],
        }

    @staticmethod
    def get_uniq_name(vd):
        return vd['name'] + '-' + uuid.uuid4().hex[:4]

    @staticmethod
    def _handle_update_create(vd, lookup):
        ori = Account.objects.filter(**lookup).first()
        if ori and ori.secret == vd.get('secret'):
            return ori, False, 'skipped'

        instance, value = Account.objects.update_or_create(defaults=vd, **lookup)
        state = 'created' if value else 'updated'
        return instance, True, state

    @staticmethod
    def _handle_skip_create(vd, lookup):
        instance, value = Account.objects.get_or_create(defaults=vd, **lookup)
        state = 'created' if value else 'skipped'
        return instance, value, state

    @staticmethod
    def _handle_err_create(vd, lookup):
        instance, value = Account.objects.get_or_create(defaults=vd, **lookup)
        if not value:
            raise serializers.ValidationError(_('Account already exists'))
        return instance, True, 'created'

    def perform_create(self, vd, handler):
        lookup = self.get_filter_lookup(vd)
        try:
            instance, changed, state = handler(vd, lookup)
        except IntegrityError:
            vd['name'] = self.get_uniq_name(vd)
            instance, changed, state = handler(vd, lookup)
        return instance, changed, state

    def get_create_handler(self, on_invalid):
        if on_invalid == 'update':
            handler = self._handle_update_create
        elif on_invalid == 'skip':
            handler = self._handle_skip_create
        else:
            handler = self._handle_err_create
        return handler

    def perform_bulk_create(self, vd):
        assets = vd.pop('assets')
        on_invalid = vd.pop('on_invalid', 'skip')
        secret_type = vd.get('secret_type', 'password')

        if not vd.get('name'):
            vd['name'] = vd.get('username')

        create_handler = self.get_create_handler(on_invalid)
        asset_ids = [asset.id for asset in assets]
        secret_type_supports = Asset.get_secret_type_assets(asset_ids, secret_type)

        _results = {}
        for asset in assets:
            if asset not in secret_type_supports:
                _results[asset] = {
                    'error': _('Asset does not support this secret type: %s') % secret_type,
                    'state': 'error',
                }
                continue

            vd = vd.copy()
            vd['asset'] = asset
            try:
                instance, changed, state = self.perform_create(vd, create_handler)
                _results[asset] = {
                    'changed': changed, 'instance': instance.id, 'state': state
                }
            except serializers.ValidationError as e:
                _results[asset] = {'error': e.detail[0], 'state': 'error'}
            except Exception as e:
                logger.exception(e)
                _results[asset] = {'error': str(e), 'state': 'error'}

        results = [{'asset': asset, **result} for asset, result in _results.items()]
        state_score = {'created': 3, 'updated': 2, 'skipped': 1, 'error': 0}
        results = sorted(results, key=lambda x: state_score.get(x['state'], 4))

        if on_invalid != 'error':
            return results

        errors = []
        errors.extend([result for result in results if result['state'] == 'error'])
        for result in results:
            if result['state'] != 'skipped':
                continue
            errors.append({
                'error': _('Account has exist'),
                'state': 'error',
                'asset': str(result['asset'])
            })
        if errors:
            raise serializers.ValidationError(errors)
        return results

    @staticmethod
    def push_accounts_if_need(results, push_now):
        if not push_now:
            return
        accounts = [str(v['instance']) for v in results if v.get('instance')]
        push_accounts_to_assets_task.delay(accounts)

    def create(self, validated_data):
        push_now = validated_data.pop('push_now', False)
        self.generate_source_data(validated_data)
        results = self.perform_bulk_create(validated_data)
        self.push_accounts_if_need(results, push_now)
        for res in results:
            res['asset'] = str(res['asset'])
        return results


class AccountSecretSerializer(SecretReadableMixin, AccountSerializer):
    class Meta(AccountSerializer.Meta):
        extra_kwargs = {
            'secret': {'write_only': False},
        }


class AccountHistorySerializer(serializers.ModelSerializer):
    secret_type = LabeledChoiceField(choices=SecretType.choices, label=_('Secret type'))
    id = serializers.IntegerField(label=_('ID'), source='history_id', read_only=True)

    class Meta:
        model = Account.history.model
        fields = ['id', 'secret', 'secret_type', 'version', 'history_date', 'history_user']
        read_only_fields = fields
        extra_kwargs = {
            'history_user': {'label': _('User')},
            'history_date': {'label': _('Date')},
        }


class AccountTaskSerializer(serializers.Serializer):
    ACTION_CHOICES = (
        ('test', 'test'),
        ('verify', 'verify'),
        ('push', 'push'),
    )
    action = serializers.ChoiceField(choices=ACTION_CHOICES, write_only=True)
    accounts = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects, required=False, allow_empty=True, many=True
    )
    task = serializers.CharField(read_only=True)
    params = serializers.JSONField(
        decoder=None, encoder=None, required=False,
        style={'base_template': 'textarea.html'}
    )
