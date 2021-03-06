from django import forms

from djangorestframework.response import ErrorResponse
from djangorestframework.serializer import Serializer
from djangorestframework.utils import as_tuple


class BaseResource(Serializer):
    """
    Base class for all Resource classes, which simply defines the interface
    they provide.
    """
    fields = None
    include = None
    exclude = None

    def __init__(self, view=None, depth=None, stack=[], **kwargs):
        super(BaseResource, self).__init__(depth, stack, **kwargs)
        self.view = view
        self.request = getattr(view, 'request', None)

    def validate_request(self, data, files=None):
        """
        Given the request content return the cleaned, validated content.
        Typically raises a :exc:`response.ErrorResponse` with status code 400
        (Bad Request) on failure.
        """
        return data

    def filter_response(self, obj):
        """
        Given the response content, filter it into a serializable object.
        """
        return self.serialize(obj)


class Resource(BaseResource):
    """
    A Resource determines how a python object maps to some serializable data.
    Objects that a resource can act on include plain Python object instances,
    Django Models, and Django QuerySets.
    """

    # The model attribute refers to the Django Model which this Resource maps to.
    # (The Model's class, rather than an instance of the Model)
    model = None

    # By default the set of returned fields will be the set of:
    #
    # 0. All the fields on the model, excluding 'id'.
    # 1. All the properties on the model.
    # 2. The absolute_url of the model, if a get_absolute_url method exists for the model.
    #
    # If you wish to override this behaviour,
    # you should explicitly set the fields attribute on your class.
    fields = None


class FormResource(Resource):
    """
    Resource class that uses forms for validation.
    Also provides a :meth:`get_bound_form` method which may be used by some renderers.

    On calling :meth:`validate_request` this validator may set a :attr:`bound_form_instance` attribute on the
    view, which may be used by some renderers.
    """

    form = None
    """
    The :class:`Form` class that should be used for request validation.
    This can be overridden by a :attr:`form` attribute on the :class:`views.View`.
    """

    allow_unknown_form_fields = False
    """
    Flag to check for unknown fields when validating a form. If set to false and
    we receive request data that is not expected by the form it raises an
    :exc:`response.ErrorResponse` with status code 400. If set to true, only
    expected fields are validated.
    """

    def validate_request(self, data, files=None):
        """
        Given some content as input return some cleaned, validated content.
        Raises a :exc:`response.ErrorResponse` with status code 400 (Bad Request) on failure.

        Validation is standard form validation, with an additional constraint that *no extra unknown fields* may be supplied
        if :attr:`self.allow_unknown_form_fields` is ``False``.

        On failure the :exc:`response.ErrorResponse` content is a dict which may contain :obj:`'errors'` and :obj:`'field-errors'` keys.
        If the :obj:`'errors'` key exists it is a list of strings of non-field errors.
        If the :obj:`'field-errors'` key exists it is a dict of ``{'field name as string': ['errors as strings', ...]}``.
        """
        return self._validate(data, files)

    def _validate(self, data, files, allowed_extra_fields=(), fake_data=None):
        """
        Wrapped by validate to hide the extra flags that are used in the implementation.

        allowed_extra_fields is a list of fields which are not defined by the form, but which we still
        expect to see on the input.

        fake_data is a string that should be used as an extra key, as a kludge to force .errors
        to be populated when an empty dict is supplied in `data`
        """

        # We'd like nice error messages even if no content is supplied.
        # Typically if an empty dict is given to a form Django will
        # return .is_valid() == False, but .errors == {}
        #
        # To get around this case we revalidate with some fake data.
        if fake_data:
            data[fake_data] = '_fake_data'
            allowed_extra_fields = tuple(allowed_extra_fields) + ('_fake_data',)

        bound_form = self.get_bound_form(data, files)

        if bound_form is None:
            return data

        self.view.bound_form_instance = bound_form

        data = data and data or {}
        files = files and files or {}

        seen_fields_set = set(data.keys())
        form_fields_set = set(bound_form.fields.keys())
        allowed_extra_fields_set = set(allowed_extra_fields)

        # In addition to regular validation we also ensure no additional fields are being passed in...
        unknown_fields = seen_fields_set - (form_fields_set | allowed_extra_fields_set)
        unknown_fields = unknown_fields - set(('csrfmiddlewaretoken', '_accept', '_method'))  # TODO: Ugh.

        # Check using both regular validation, and our stricter no additional fields rule
        if bound_form.is_valid() and (self.allow_unknown_form_fields or not unknown_fields):
            # Validation succeeded...
            cleaned_data = bound_form.cleaned_data

            # Add in any extra fields to the cleaned content...
            for key in (allowed_extra_fields_set & seen_fields_set) - set(cleaned_data.keys()):
                cleaned_data[key] = data[key]

            return cleaned_data

        # Validation failed...
        detail = {}

        if not bound_form.errors and not unknown_fields:
            # is_valid() was False, but errors was empty.
            # If we havn't already done so attempt revalidation with some fake data
            # to force django to give us an errors dict.
            if fake_data is None:
                return self._validate(data, files, allowed_extra_fields, '_fake_data')

            # If we've already set fake_dict and we're still here, fallback gracefully.
            detail = {u'errors': [u'No content was supplied.']}

        else:
            # Add any non-field errors
            if bound_form.non_field_errors():
                detail[u'errors'] = bound_form.non_field_errors()

            # Add standard field errors
            field_errors = dict(
                (key, map(unicode, val))
                for (key, val)
                in bound_form.errors.iteritems()
                if not key.startswith('__')
            )

            # Add any unknown field errors
            for key in unknown_fields:
                field_errors[key] = [u'This field does not exist.']

            if field_errors:
                detail[u'field_errors'] = field_errors

        # Return HTTP 400 response (BAD REQUEST)
        raise ErrorResponse(400, detail)

    def get_form_class(self, method=None):
        """
        Returns the form class used to validate this resource.
        """
        # A form on the view overrides a form on the resource.
        form = getattr(self.view, 'form', None) or self.form

        # Use the requested method or determine the request method
        if method is None and hasattr(self.view, 'request') and hasattr(self.view, 'method'):
            method = self.view.method
        elif method is None and hasattr(self.view, 'request'):
            method = self.view.request.method

        # A method form on the view or resource overrides the general case.
        # Method forms are attributes like `get_form` `post_form` `put_form`.
        if method:
            form = getattr(self, '%s_form' % method.lower(), form)
            form = getattr(self.view, '%s_form' % method.lower(), form)

        return form

    def get_bound_form(self, data=None, files=None, method=None):
        """
        Given some content return a Django form bound to that content.
        If form validation is turned off (:attr:`form` class attribute is :const:`None`) then returns :const:`None`.
        """
        form = self.get_form_class(method)

        if not form:
            return None

        if data is not None or files is not None:
            return form(data, files)

        return form()


class ModelResource(FormResource):
    """
    Resource class that uses forms for validation and otherwise falls back to a model form if no form is set.
    Also provides a :meth:`get_bound_form` method which may be used by some renderers.
    """

    form = None
    """
    The form class that should be used for request validation.
    If set to :const:`None` then the default model form validation will be used.

    This can be overridden by a :attr:`form` attribute on the :class:`views.View`.
    """

    model = None
    """
    The model class which this resource maps to.

    This can be overridden by a :attr:`model` attribute on the :class:`views.View`.
    """

    fields = None
    """
    The list of fields to use on the output.

    May be any of:

    The name of a model field. To view nested resources, give the field as a tuple of ("fieldName", resource) where `resource` may be any of ModelResource reference, the name of a ModelResourc reference as a string or a tuple of strings representing fields on the nested model.
    The name of an attribute on the model.
    The name of an attribute on the resource.
    The name of a method on the model, with a signature like ``func(self)``.
    The name of a method on the resource, with a signature like ``func(self, instance)``.
    """

    exclude = ('id', 'pk')
    """
    The list of fields to exclude.  This is only used if :attr:`fields` is not set.
    """

    include = ()
    """
    The list of extra fields to include.  This is only used if :attr:`fields` is not set.
    """

    def __init__(self, view=None, depth=None, stack=[], **kwargs):
        """
        Allow :attr:`form` and :attr:`model` attributes set on the
        :class:`View` to override the :attr:`form` and :attr:`model`
        attributes set on the :class:`Resource`.
        """
        super(ModelResource, self).__init__(view, depth, stack, **kwargs)

        self.model = getattr(view, 'model', None) or self.model

    def validate_request(self, data, files=None):
        """
        Given some content as input return some cleaned, validated content.
        Raises a :exc:`response.ErrorResponse` with status code 400 (Bad Request) on failure.

        Validation is standard form or model form validation,
        with an additional constraint that no extra unknown fields may be supplied,
        and that all fields specified by the fields class attribute must be supplied,
        even if they are not validated by the form/model form.

        On failure the ErrorResponse content is a dict which may contain :obj:`'errors'` and :obj:`'field-errors'` keys.
        If the :obj:`'errors'` key exists it is a list of strings of non-field errors.
        If the ''field-errors'` key exists it is a dict of {field name as string: list of errors as strings}.
        """
        return self._validate(data, files, allowed_extra_fields=self._property_fields_set)

    def get_bound_form(self, data=None, files=None, method=None):
        """
        Given some content return a ``Form`` instance bound to that content.

        If the :attr:`form` class attribute has been explicitly set then that class will be used
        to create the Form, otherwise the model will be used to create a ModelForm.
        """
        form = self.get_form_class(method)

        if not form and self.model:
            # Fall back to ModelForm which we create on the fly
            class OnTheFlyModelForm(forms.ModelForm):
                class Meta:
                    model = self.model
                    #fields = tuple(self._model_fields_set)

            form = OnTheFlyModelForm

        # Both form and model not set?  Okay bruv, whatevs...
        if not form:
            return None

        # Instantiate the ModelForm as appropriate
        if data is not None or files is not None:
            if issubclass(form, forms.ModelForm) and hasattr(self.view, 'model_instance'):
                # Bound to an existing model instance
                return form(data, files, instance=self.view.model_instance)
            else:
                return form(data, files)

        return form()

    @property
    def _model_fields_set(self):
        """
        Return a set containing the names of validated fields on the model.
        """
        model_fields = set(field.name for field in self.model._meta.fields)

        if self.fields:
            return model_fields & set(as_tuple(self.fields))

        return model_fields - set(as_tuple(self.exclude))

    @property
    def _property_fields_set(self):
        """
        Returns a set containing the names of validated properties on the model.
        """
        property_fields = set(attr for attr in dir(self.model) if
                              isinstance(getattr(self.model, attr, None), property)
                              and not attr.startswith('_'))

        if self.fields:
            return property_fields & set(as_tuple(self.fields))

        return property_fields.union(set(as_tuple(self.include))) - set(as_tuple(self.exclude))
