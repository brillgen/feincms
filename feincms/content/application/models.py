"""
Third-party application inclusion support.
"""

from __future__ import absolute_import, unicode_literals

from email.utils import parsedate
from time import mktime
from random import SystemRandom
import re

from django.conf import settings
from django.core.cache import cache
from django.core.urlresolvers import (
    Resolver404, resolve, reverse, NoReverseMatch)
from django.db import models
from django.db.models import signals
from django.http import HttpResponse
from django.utils.functional import curry as partial, lazy, wraps
from django.utils.http import http_date
from django.utils.safestring import mark_safe
from django.utils.translation import get_language, ugettext_lazy as _

from feincms.admin.item_editor import ItemEditorForm
from feincms.contrib.fields import JSONField
from feincms.translations import short_language_code
from feincms.utils import get_object


def cycle_app_reverse_cache(*args, **kwargs):
    """Does not really empty the cache; instead it adds a random element to the
    cache key generation which guarantees that the cache does not yet contain
    values for all newly generated keys"""
    cache.set('app_reverse_cache_generation', str(SystemRandom().random()))


# Set the app_reverse_cache_generation value once per startup (at least).
# This protects us against offline modifications of the database.
cycle_app_reverse_cache()


def app_reverse(viewname, urlconf=None, args=None, kwargs=None, prefix=None,
                *vargs, **vkwargs):
    """
    Reverse URLs from application contents

    Works almost like Django's own reverse() method except that it resolves
    URLs from application contents. The second argument, ``urlconf``, has to
    correspond to the URLconf parameter passed in the ``APPLICATIONS`` list
    to ``Page.create_content_type``::

        app_reverse('mymodel-detail', 'myapp.urls', args=...)

        or

        app_reverse('mymodel-detail', 'myapp.urls', kwargs=...)

    The second argument may also be a request object if you want to reverse
    an URL belonging to the current application content.
    """

    # First parameter might be a request instead of an urlconf path, so
    # we'll try to be helpful and extract the current urlconf from it
    extra_context = getattr(urlconf, '_feincms_extra_context', {})
    appconfig = extra_context.get('app_config', {})
    urlconf = appconfig.get('urlconf_path', urlconf)

    cache_generation = cache.get('app_reverse_cache_generation')
    if cache_generation is None:
        # This might never happen. Still, better be safe than sorry.
        cycle_app_reverse_cache()
        cache_generation = cache.get('app_reverse_cache_generation')

    cache_key = '%s-%s-%s-%s' % (
        urlconf,
        get_language(),
        getattr(settings, 'SITE_ID', 0),
        cache_generation)

    url_prefix = cache.get(cache_key)

    if url_prefix is None:
        appcontent_class = ApplicationContent._feincms_content_models[0]
        content = appcontent_class.closest_match(urlconf)

        if content is not None:
            if urlconf in appcontent_class.ALL_APPS_CONFIG:
                # We have an overridden URLconf
                app_config = appcontent_class.ALL_APPS_CONFIG[urlconf]
                urlconf = app_config['config'].get('urls', urlconf)

            prefix = content.parent.get_absolute_url()
            prefix += '/' if prefix[-1] != '/' else ''

            url_prefix = (urlconf, prefix)
            cache.set(cache_key, url_prefix)

    if url_prefix:
        # vargs and vkwargs are used to send through additional parameters
        # which are uninteresting to us (such as current_app)
        return reverse(
            viewname,
            url_prefix[0],
            args=args,
            kwargs=kwargs,
            prefix=url_prefix[1],
            *vargs, **vkwargs)

    raise NoReverseMatch("Unable to find ApplicationContent for %r" % urlconf)


#: Lazy version of ``app_reverse``
app_reverse_lazy = lazy(app_reverse, str)


def permalink(func):
    """
    Decorator that calls app_reverse()

    Use this instead of standard django.db.models.permalink if you want to
    integrate the model through ApplicationContent. The wrapped function
    must return 4 instead of 3 arguments::

        class MyModel(models.Model):
            @appmodels.permalink
            def get_absolute_url(self):
                return ('myapp.urls', 'model_detail', (), {'slug': self.slug})
    """
    def inner(*args, **kwargs):
        return app_reverse(*func(*args, **kwargs))
    return wraps(func)(inner)


APPLICATIONCONTENT_RE = re.compile(r'^([^/]+)/([^/]+)$')


class ApplicationContent(models.Model):
    #: parameters is used to serialize instance-specific data which will be
    # provided to the view code. This allows customization (e.g. "Embed
    # MyBlogApp for blog <slug>")
    parameters = JSONField(null=True, editable=False)

    ALL_APPS_CONFIG = {}

    class Meta:
        abstract = True
        verbose_name = _('application content')
        verbose_name_plural = _('application contents')

    @classmethod
    def initialize_type(cls, APPLICATIONS):
        for i in APPLICATIONS:
            if not 2 <= len(i) <= 3:
                raise ValueError(
                    "APPLICATIONS must be provided with tuples containing at"
                    " least two parameters (urls, name) and an optional extra"
                    " config dict")

            urls, name = i[0:2]

            if len(i) == 3:
                app_conf = i[2]

                if not isinstance(app_conf, dict):
                    raise ValueError(
                        "The third parameter of an APPLICATIONS entry must be"
                        " a dict or the name of one!")
            else:
                app_conf = {}

            cls.ALL_APPS_CONFIG[urls] = {
                "urls": urls,
                "name": name,
                "config": app_conf
            }

        cls.add_to_class(
            'urlconf_path',
            models.CharField(_('application'), max_length=100, choices=[
                (c['urls'], c['name']) for c in cls.ALL_APPS_CONFIG.values()])
        )

        class ApplicationContentItemEditorForm(ItemEditorForm):
            app_config = {}
            custom_fields = {}

            def __init__(self, *args, **kwargs):
                super(ApplicationContentItemEditorForm, self).__init__(
                    *args, **kwargs)

                instance = kwargs.get("instance", None)

                if instance:
                    try:
                        # TODO use urlconf_path from POST if set
                        # urlconf_path = request.POST.get('...urlconf_path',
                        #     instance.urlconf_path)
                        self.app_config = cls.ALL_APPS_CONFIG[
                            instance.urlconf_path]['config']
                    except KeyError:
                        self.app_config = {}

                    self.custom_fields = {}
                    admin_fields = self.app_config.get('admin_fields', {})

                    if isinstance(admin_fields, dict):
                        self.custom_fields.update(admin_fields)
                    else:
                        get_fields = get_object(admin_fields)
                        self.custom_fields.update(
                            get_fields(self, *args, **kwargs))

                    params = self.instance.parameters
                    for k, v in self.custom_fields.items():
                        v.initial = params.get(k)
                        self.fields[k] = v
                        if k in params:
                            self.fields[k].initial = params[k]

            def save(self, commit=True, *args, **kwargs):
                # Django ModelForms return the model instance from save. We'll
                # call save with commit=False first to do any necessary work &
                # get the model so we can set .parameters to the values of our
                # custom fields before calling save(commit=True)

                m = super(ApplicationContentItemEditorForm, self).save(
                    commit=False, *args, **kwargs)

                m.parameters = dict(
                    (k, self.cleaned_data[k])
                    for k in self.custom_fields if k in self.cleaned_data)

                if commit:
                    m.save(**kwargs)

                return m

        # This provides hooks for us to customize the admin interface for
        # embedded instances:
        cls.feincms_item_editor_form = ApplicationContentItemEditorForm

        # Clobber the app_reverse cache when saving application contents
        # and/or pages
        page_class = cls.parent.field.rel.to
        signals.post_save.connect(cycle_app_reverse_cache, sender=cls)
        signals.post_delete.connect(cycle_app_reverse_cache, sender=cls)
        signals.post_save.connect(cycle_app_reverse_cache, sender=page_class)
        signals.post_delete.connect(cycle_app_reverse_cache, sender=page_class)

    def __init__(self, *args, **kwargs):
        super(ApplicationContent, self).__init__(*args, **kwargs)
        self.app_config = self.ALL_APPS_CONFIG.get(
            self.urlconf_path, {}).get('config', {})

    def process(self, request, **kw):
        page_url = self.parent.get_absolute_url()

        # Provide a way for appcontent items to customize URL processing by
        # altering the perceived path of the page:
        if "path_mapper" in self.app_config:
            path_mapper = get_object(self.app_config["path_mapper"])
            path, page_url = path_mapper(
                request.path,
                page_url,
                appcontent_parameters=self.parameters
            )
        else:
            path = request._feincms_extra_context['extra_path']

        # Resolve the module holding the application urls.
        urlconf_path = self.app_config.get('urls', self.urlconf_path)

        try:
            fn, args, kwargs = resolve(path, urlconf_path)
        except (ValueError, Resolver404):
            raise Resolver404(str('Not found (resolving %r in %r failed)') % (
                path, urlconf_path))

        # Variables from the ApplicationContent parameters are added to request
        # so we can expose them to our templates via the appcontent_parameters
        # context_processor
        request._feincms_extra_context.update(self.parameters)

        # Save the application configuration for reuse elsewhere
        request._feincms_extra_context.update({
            'app_config': dict(
                self.app_config,
                urlconf_path=self.urlconf_path,
            ),
        })

        view_wrapper = self.app_config.get("view_wrapper", None)
        if view_wrapper:
            fn = partial(
                get_object(view_wrapper),
                view=fn,
                appcontent_parameters=self.parameters
            )

        output = fn(request, *args, **kwargs)

        if isinstance(output, HttpResponse):
            if self.send_directly(request, output):
                return output
            elif output.status_code == 200:

                # If the response supports deferred rendering, render the
                # response right now. We do not handle template response
                # middleware.
                if hasattr(output, 'render') and callable(output.render):
                    output.render()

                self.rendered_result = mark_safe(
                    output.content.decode('utf-8'))
                self.rendered_headers = {}

                # Copy relevant headers for later perusal
                for h in ('Cache-Control', 'Last-Modified', 'Expires'):
                    if h in output:
                        self.rendered_headers.setdefault(
                            h, []).append(output[h])

        elif isinstance(output, tuple) and 'view' in kw:
            kw['view'].template_name = output[0]
            kw['view'].request._feincms_extra_context.update(output[1])
        else:
            self.rendered_result = mark_safe(output)

        return True  # successful

    def send_directly(self, request, response):
        mimetype = response.get('Content-Type', 'text/plain')
        if ';' in mimetype:
            mimetype = mimetype.split(';')[0]
        mimetype = mimetype.strip()

        return (response.status_code != 200
                or request.is_ajax()
                or getattr(response, 'standalone', False)
                or mimetype not in ('text/html', 'text/plain'))

    def render(self, **kwargs):
        return getattr(self, 'rendered_result', '')

    def finalize(self, request, response):
        headers = getattr(self, 'rendered_headers', None)
        if headers:
            self._update_response_headers(request, response, headers)

    def _update_response_headers(self, request, response, headers):
        """
        Combine all headers that were set by the different content types
        We are interested in Cache-Control, Last-Modified, Expires
        """

        # Ideally, for the Cache-Control header, we'd want to do some
        # intelligent combining, but that's hard. Let's just collect and unique
        # them and let the client worry about that.
        cc_headers = set(('must-revalidate',))
        for x in (cc.split(",") for cc in headers.get('Cache-Control', ())):
            cc_headers |= set((s.strip() for s in x))

        if len(cc_headers):
            response['Cache-Control'] = ", ".join(cc_headers)
        else:   # Default value
            response['Cache-Control'] = 'no-cache, must-revalidate'

        # Check all Last-Modified headers, choose the latest one
        lm_list = [parsedate(x) for x in headers.get('Last-Modified', ())]
        if len(lm_list) > 0:
            response['Last-Modified'] = http_date(mktime(max(lm_list)))

        # Check all Expires headers, choose the earliest one
        lm_list = [parsedate(x) for x in headers.get('Expires', ())]
        if len(lm_list) > 0:
            response['Expires'] = http_date(mktime(min(lm_list)))

    @classmethod
    def closest_match(cls, urlconf_path):
        page_class = cls.parent.field.rel.to

        contents = cls.objects.filter(
            parent__in=page_class.objects.active(),
            urlconf_path=urlconf_path,
        ).order_by('pk').select_related('parent')

        if len(contents) > 1:
            try:
                current = short_language_code(get_language())
                return [
                    content for content in contents if
                    short_language_code(content.parent.language) == current
                ][0]

            except (AttributeError, IndexError):
                pass

        try:
            return contents[0]
        except IndexError:
            pass

        return None
