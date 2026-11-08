import type { ReactNode } from "react";

type PageHeadingProps = {
  title: string;
  description: string;
  className: string;
  titleId?: string;
  descriptionClassName?: string;
  as?: "div" | "header";
  children?: ReactNode;
};

export function PageHeading({
  title,
  description,
  className,
  titleId,
  descriptionClassName,
  as: Element = "div",
  children,
}: PageHeadingProps) {
  return (
    <Element className={className}>
      <div>
        <h1 id={titleId}>{title}</h1>
        <p className={descriptionClassName}>{description}</p>
      </div>
      {children}
    </Element>
  );
}
