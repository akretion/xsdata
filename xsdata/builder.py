from dataclasses import dataclass
from dataclasses import field
from typing import Iterator
from typing import List
from typing import Optional
from typing import Union

from xsdata.logger import logger
from xsdata.models.codegen import Attr
from xsdata.models.codegen import AttrType
from xsdata.models.codegen import Class
from xsdata.models.codegen import Extension
from xsdata.models.codegen import Restrictions
from xsdata.models.elements import Attribute
from xsdata.models.elements import AttributeGroup
from xsdata.models.elements import ComplexType
from xsdata.models.elements import Element
from xsdata.models.elements import Enumeration
from xsdata.models.elements import Group
from xsdata.models.elements import List as ListElement
from xsdata.models.elements import Redefine
from xsdata.models.elements import Restriction
from xsdata.models.elements import Schema
from xsdata.models.elements import SimpleType
from xsdata.models.elements import Union as UnionElement
from xsdata.models.enums import DataType
from xsdata.models.enums import Namespace
from xsdata.models.enums import TagType
from xsdata.models.mixins import ElementBase
from xsdata.models.mixins import NamedField
from xsdata.utils import text

BaseElement = Union[Attribute, AttributeGroup, Element, ComplexType, SimpleType, Group]
AttributeElement = Union[
    Attribute, Element, Restriction, Enumeration, UnionElement, ListElement, SimpleType
]


@dataclass
class ClassBuilder:
    schema: Schema
    redefine: Optional[Redefine] = field(default=None)
    target_prefix: Optional[str] = field(init=False)

    def build(self) -> List[Class]:
        """Generate classes from schema and redefined elements."""
        classes: List[Class] = []

        for override in self.schema.overrides:
            classes.extend(map(self.build_class, override.children()))

        classes.extend(map(self.build_class, self.schema.simple_types))
        classes.extend(map(self.build_class, self.schema.attribute_groups))
        classes.extend(map(self.build_class, self.schema.groups))
        classes.extend(map(self.build_class, self.schema.attributes))
        classes.extend(map(self.build_class, self.schema.complex_types))
        classes.extend(map(self.build_class, self.schema.elements))

        if self.redefine:
            classes.extend(map(self.build_class, self.redefine.children()))

        return classes

    def build_class(self, obj: BaseElement) -> Class:
        """Build and return a class instance."""
        namespace = self.element_namespace(obj)
        instance = Class(
            name=obj.real_name,
            is_abstract=obj.is_abstract,
            namespace=namespace,
            is_mixed=obj.is_mixed,
            type=type(obj),
            help=obj.display_help,
        )

        self.build_class_extensions(obj, instance)
        self.build_class_attributes(obj, instance)
        return instance

    def build_class_attributes(self, obj: ElementBase, instance: Class):
        """Build the instance class attributes from the given ElementBase
        children."""
        for child in self.element_children(obj):
            self.build_class_attribute(instance, child)

        attr = self.default_class_attribute(instance)
        if attr:
            instance.attrs.append(attr)

        instance.attrs.sort(key=lambda x: x.index)

    def build_class_extensions(self, obj: ElementBase, instance: Class):
        """Build the item class extensions from the given ElementBase
        children."""
        extensions = dict()
        if getattr(obj, "type", None):
            name = getattr(obj, "type", None)
            extension = self.build_class_extension(instance, name, 0, dict())
            extensions[name] = extension

        for extension in self.children_extensions(obj, instance):
            extension.forward_ref = False
            extensions[extension.type.name] = extension

        instance.extensions = sorted(extensions.values(), key=lambda x: x.type.name)

    def build_data_type(
        self, instance: Class, name: str, index: int = 0, forward_ref: bool = False
    ) -> AttrType:
        prefix, suffix = text.split(name)
        native = False
        self_ref = False
        namespace = self.schema.nsmap.get(prefix)

        if prefix == Namespace.XML.prefix or namespace == Namespace.SCHEMA.uri:
            name = suffix
            native = True
        elif namespace == self.schema.target_namespace:
            if suffix == instance.name:
                self_ref = True

        return AttrType(
            name=name,
            index=index,
            native=native,
            forward_ref=forward_ref,
            self_ref=self_ref,
        )

    def element_children(self, obj: ElementBase) -> Iterator[AttributeElement]:
        """Recursively find and return all child elements that are qualified to
        be class attributes."""
        for child in obj.children():
            if child.is_attribute:
                yield child
            else:
                yield from self.element_children(child)

    def element_namespace(self, obj: NamedField) -> Optional[str]:
        """
        Returns an element's namespace by its prefix and form.

        Examples:
            - prefixed elements returns the namespace from schema nsmap
            - qualified elements returns the schema target namespace
            - unqualified elements return an empty string
            - unqualified attributes return None
        """

        prefix = obj.prefix
        if prefix:
            return self.schema.nsmap.get(prefix)
        elif obj.is_qualified:
            return self.schema.target_namespace
        elif isinstance(obj, Element):
            return ""

        return None

    def children_extensions(
        self, obj: ElementBase, instance: Class
    ) -> Iterator[Extension]:
        """
        Recursively find and return all instance's Extension classes.

        If the initial given obj has a type attribute include it in
        result.
        """
        for child in obj.children():
            if child.is_attribute:
                continue

            for ext in child.extensions:
                yield self.build_class_extension(
                    instance, ext, child.index, child.get_restrictions()
                )

            yield from self.children_extensions(child, instance)

    def build_class_extension(self, instance, name, index, restrictions):
        return Extension(
            type=self.build_data_type(instance, name, index=index),
            restrictions=Restrictions(**restrictions),
        )

    def build_class_attribute(self, instance: Class, obj: AttributeElement):
        """
        Generate and append an attribute instance to the instance class.

        :raise ValueError:  if types list is empty
        """
        types = self.build_class_attribute_types(instance, obj)
        restrictions = Restrictions(**obj.get_restrictions())
        instance.attrs.append(
            Attr(
                index=obj.index,
                name=obj.real_name,
                default=obj.default_value,
                fixed=obj.is_fixed,
                types=types,
                local_type=obj.class_name,
                help=obj.display_help,
                namespace=self.element_namespace(obj),
                restrictions=restrictions,
            )
        )

    def build_class_attribute_types(
        self, instance: Class, obj: AttributeElement
    ) -> List[AttrType]:
        """Convert real type and anonymous inner elements to an attribute type
        list."""
        inner_class = self.build_inner_class(obj)

        types = [
            self.build_data_type(instance, name)
            for name in (obj.real_type or "").split(" ")
            if name
        ]

        if inner_class:
            instance.inner.append(inner_class)
            types.append(AttrType(name=inner_class.name, forward_ref=True))

        if len(types) == 0:
            types.append(AttrType(name=DataType.STRING.code, native=True))

        return types

    def build_inner_class(self, obj: AttributeElement) -> Optional[Class]:
        """Find and convert anonymous types to a class instance."""
        if self.has_anonymous_class(obj):
            complex_type = obj.complex_type  # type: ignore
            complex_type.name = obj.name  # type: ignore
            obj.complex_type = None  # type: ignore
            return self.build_class(complex_type)  # type: ignore

        if self.has_anonymous_enumeration(obj):
            simple_type = obj.simple_type  # type: ignore
            simple_type.name = obj.real_name  # type: ignore
            obj.type = None  # type: ignore
            obj.simple_type = None  # type: ignore
            return self.build_class(simple_type)  # type: ignore

        if isinstance(obj, SimpleType) and obj.is_enumeration:
            obj.name = "type"
            inner = self.build_class(obj)
            obj.name = "value"
            obj.restriction = None
            return inner

        if isinstance(obj, UnionElement) and obj.simple_types:
            # Only the last occurrence will be converted. It doesn't make sense
            # to have a union with two anonymous enumerations.
            for i in range(len(obj.simple_types) - 1, -1, -1):
                if obj.simple_types[i].is_enumeration:
                    simple_type = obj.simple_types.pop(i)
                    simple_type.name = obj.real_name
                    return self.build_class(simple_type)

        return None

    @staticmethod
    def has_anonymous_class(obj: AttributeElement) -> bool:
        """Check if the attribute element contains anonymous complex type."""
        return (
            isinstance(obj, Element)
            and obj.real_type is None
            and obj.complex_type is not None
        )

    @staticmethod
    def has_anonymous_enumeration(obj: AttributeElement) -> bool:
        """Check if the attribute element contains anonymous restriction with
        enumeration facet."""
        return (
            isinstance(obj, (Attribute, Element))
            and obj.type is None
            and obj.simple_type is not None
            and obj.simple_type.is_enumeration
        )

    @staticmethod
    def default_class_attribute(instance: Class) -> Optional[Attr]:
        types = []
        restrictions = Restrictions()
        if len(instance.extensions) == 0 and len(instance.attrs) == 0:
            types.append(AttrType(name=DataType.STRING.code, native=True))
            logger.warning(f"Empty class: `{instance.name}`")
        elif not instance.is_enumeration:
            for i in range(len(instance.extensions) - 1, -1, -1):
                if instance.extensions[i].type.native:
                    extension = instance.extensions.pop(i)
                    types.insert(0, extension.type)
                    restrictions.update(extension.restrictions)

        if types:
            return Attr(
                name="value",
                index=0,
                default=None,
                types=types,
                local_type=TagType.EXTENSION,
                restrictions=restrictions,
            )
        return None
