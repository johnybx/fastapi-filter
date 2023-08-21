from collections import defaultdict
from copy import deepcopy
from types import UnionType
from typing import Annotated, Any, Dict, Iterable, List, Optional, Tuple, Type, Union, get_args, get_origin

from fastapi import Depends
from fastapi.exceptions import RequestValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    FieldValidationInfo,
    PlainValidator,
    ValidationError,
    create_model,
    field_validator,
)
from pydantic.fields import FieldInfo


class BaseFilterModel(BaseModel, extra="forbid"):
    """Abstract base filter class.

    Provides the interface for filtering and ordering.


    # Ordering

    ## Query string examples:

        >>> "?order_by=-created_at"
        >>> "?order_by=created_at,updated_at"
        >>> "?order_by=+created_at,-name"

    ## Limitation

    Sorting doesn't support related fields, you can only use the attributes of the targeted model/collection.
    For example, you can't use `related_model__attribute`.

    # Filtering

    ## Query string examples:

        >>> "?my_field__gt=12&my_other_field=Tomato"
        >>> "?my_field__in=12,13,15&my_other_field__not_in=Tomato,Pepper"
    """

    class Constants:  # pragma: no cover
        model: Type
        ordering_field_name: str = "order_by"
        search_model_fields: List[str]
        search_field_name: str = "search"
        prefix: str

    def filter(self, query):  # pragma: no cover
        ...

    @property
    def filtering_fields(self):
        fields = self.model_dump(exclude_none=True, exclude_unset=True)
        fields.pop(self.Constants.ordering_field_name, None)
        return fields.items()

    def sort(self, query):  # pragma: no cover
        ...

    @property
    def ordering_values(self):
        """Check that the ordering field is present on the class definition."""
        try:
            return getattr(self, self.Constants.ordering_field_name)
        except AttributeError as e:
            raise AttributeError(
                f"Ordering field {self.Constants.ordering_field_name} is not defined. "
                "Make sure to add it to your filter class."
            ) from e

    @field_validator("*", mode="before", check_fields=False)
    def strip_order_by_values(cls, value, field: FieldValidationInfo):
        if field.field_name != cls.Constants.ordering_field_name:
            return value

        if not value:
            return None

        stripped_values = []
        for field_name in value:
            stripped_value = field_name.strip()
            if stripped_value:
                stripped_values.append(stripped_value)

        return stripped_values

    @field_validator("*", mode="before", check_fields=False)
    def validate_order_by(cls, value, field: FieldValidationInfo):
        if field.field_name != cls.Constants.ordering_field_name:
            return value

        if not value:
            return None

        field_name_usages = defaultdict(list)
        duplicated_field_names = set()

        for field_name_with_direction in value:
            field_name = field_name_with_direction.replace("-", "").replace("+", "")

            if not hasattr(cls.Constants.model, field_name):
                raise ValueError(f"{field_name} is not a valid ordering field.")

            field_name_usages[field_name].append(field_name_with_direction)
            if len(field_name_usages[field_name]) > 1:
                duplicated_field_names.add(field_name)

        if duplicated_field_names:
            ambiguous_field_names = ", ".join(
                [
                    field_name_with_direction
                    for field_name in sorted(duplicated_field_names)
                    for field_name_with_direction in field_name_usages[field_name]
                ]
            )
            raise ValueError(
                f"Field names can appear at most once for {cls.Constants.ordering_field_name}. "
                f"The following was ambiguous: {ambiguous_field_names}."
            )

        return value


def with_prefix(prefix: str, Filter: Type[BaseFilterModel]):
    """Allow re-using existing filter under a prefix.

    Example:
        ```python
        from pydantic import BaseModel

        from fastapi_filter.filter import FilterDepends

        class NumberFilter(BaseModel):
            count: Optional[int]

        number_filter_prefixed, Annotation = with_prefix("number_filter", Filter)
        class MainFilter(BaseModel):
            name: str
            number_filter: Optional[Annotation] = FilterDepends(number_filter_prefixed)
        ```

    As a result, you'll get the following filters:
        * name
        * number_filter__count

    # Limitation

    The alias generator is the last to be picked in order of prevalence. So if one of the fields has a `Query` as
    default and declares an alias already, this will be picked first and you won't get the prefix.

    Example:
        ```python
         from pydantic import BaseModel

        class NumberFilter(BaseModel):
            count: Optional[int] = Query(default=10, alias=counter)

        number_filter_prefixed, Annotation = with_prefix("number_filter", Filter)
        class MainFilter(BaseModel):
            name: str
            number_filter: Optional[Annotation] = FilterDepends(number_filter_prefixed)
        ```

    As a result, you'll get the following filters:
        * name
        * counter (*NOT* number_filter__counter)
    """

    class NestedFilter(Filter):  # type: ignore[misc, valid-type]
        model_config = ConfigDict(extra="forbid", alias_generator=lambda string: f"{prefix}__{string}")

        class Constants(Filter.Constants):  # type: ignore[name-defined]
            ...

    NestedFilter.Constants.prefix = prefix

    def plain_validator(value):
        # Make sure we validate Model.
        # Probably would be better if this was subclass of specific Filter but
        if issubclass(value.__class__, BaseModel):
            value = value.model_dump()

        if isinstance(value, dict):
            stripped = {k.removeprefix(NestedFilter.Constants.prefix): v for k, v in value.items()}
            return Filter(**stripped)

        raise ValueError(f"Unexpected type: {type(value)}")

    annotation = Annotated[Filter, PlainValidator(plain_validator)]

    return NestedFilter, annotation


def _list_to_str_fields(Filter: Type[BaseFilterModel]):
    ret: Dict[str, Tuple[Union[object, Type], Optional[FieldInfo]]] = {}
    for name, f in Filter.model_fields.items():
        field_info = deepcopy(f)
        annotation = f.annotation

        if get_origin(annotation) in [UnionType, Union]:
            annotation_args: list = list(get_args(f.annotation))
            if type(None) in annotation_args:
                annotation_args.remove(type(None))
            if len(annotation_args) == 1:
                annotation = annotation_args[0]
            # Not sure what to do if there is more then 1 value 🤔
            # Do we need to handle Optional[Annotated[...]] ?

        if annotation is list or get_origin(annotation) is list:
            if isinstance(field_info.default, Iterable):
                field_info.default = ",".join(field_info.default)
            ret[name] = (str if f.is_required() else Optional[str], field_info)
        else:
            ret[name] = (f.annotation, field_info)

    return ret


def FilterDepends(Filter: Type[BaseFilterModel], *, by_alias: bool = False, use_cache: bool = True) -> Any:
    """Use a hack to support lists in filters.

    FastAPI doesn't support it yet: https://github.com/tiangolo/fastapi/issues/50

    What we do is loop through the fields of a filter and change any `list` field to a `str` one so that it won't be
    excluded from the possible query parameters.

    When we apply the filter, we build the original filter to properly validate the data (i.e. can the string be parsed
    and formatted as a list of <type>?)
    """
    fields = _list_to_str_fields(Filter)
    GeneratedFilter: Type[BaseFilterModel] = create_model(Filter.__class__.__name__, **fields)

    class FilterWrapper(GeneratedFilter):  # type: ignore[misc,valid-type]
        def filter(self, *args, **kwargs):
            try:
                original_filter = Filter(**self.model_dump(by_alias=by_alias))
            except ValidationError as e:
                raise RequestValidationError(e.errors()) from e
            return original_filter.filter(*args, **kwargs)

        def sort(self, *args, **kwargs):
            try:
                original_filter = Filter(**self.model_dump(by_alias=by_alias))
            except ValidationError as e:
                raise RequestValidationError(e.errors()) from e
            return original_filter.sort(*args, **kwargs)

    return Depends(FilterWrapper)
