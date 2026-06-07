import * as React from "react"

import {
  Button as ShadButton,
  type ButtonProps as ShadButtonProps,
} from "@/components/ui/button"
import { cn } from "@/lib/utils"

type ButtonSize = "sm" | "md" | "lg" | "icon"
type ButtonVariant = NonNullable<ShadButtonProps["variant"]> | "subtle"

export interface ButtonProps
  extends Omit<ShadButtonProps, "size" | "variant"> {
  size?: ButtonSize
  variant?: ButtonVariant
}

const shadSizeBySize: Record<ButtonSize, ShadButtonProps["size"]> = {
  sm: "sm",
  md: "default",
  lg: "lg",
  icon: "icon",
}

const sizeClasses: Record<ButtonSize, string> = {
  sm: "h-7 rounded-md px-2.5 text-[12px]",
  md: "h-8 rounded-md px-3 text-[13px]",
  lg: "h-9 rounded-md px-4 text-[13px]",
  icon: "h-8 w-8 rounded-md p-0",
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "default", size = "md", ...props }, ref) => {
    const shadVariant = variant === "subtle" ? "secondary" : variant

    return (
      <ShadButton
        ref={ref}
        variant={shadVariant}
        size={shadSizeBySize[size]}
        className={cn(
          "gap-1.5 shadow-none",
          sizeClasses[size],
          variant === "outline" && "border-slate-200 bg-white text-slate-700 hover:bg-slate-50",
          variant === "ghost" && "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
          variant === "subtle" && "bg-slate-100 text-slate-700 hover:bg-slate-200",
          variant === "destructive" && "border border-red-200 bg-white text-red-600 hover:bg-red-50",
          className,
        )}
        {...props}
      />
    )
  },
)
Button.displayName = "Button"
