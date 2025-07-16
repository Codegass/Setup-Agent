#!/usr/bin/env python3
"""
Claude Thinking 功能使用示例

这个示例展示了如何在 SAG 中使用 Claude 的 thinking 功能。
Claude 的 thinking 功能允许模型进行更深入的推理，类似于 OpenAI 的 o1 模型。

使用方法:
1. 设置环境变量 ANTHROPIC_API_KEY
2. 配置 .env 文件中的 Claude 相关设置
3. 运行此示例
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

def setup_claude_thinking_config():
    """设置 Claude thinking 配置示例"""
    
    # 示例配置
    config_example = {
        "SAG_THINKING_MODEL": "claude-sonnet-4-20250514",
        "SAG_THINKING_PROVIDER": "anthropic", 
        "SAG_REASONING_EFFORT": "medium",  # low, medium, high
        "SAG_THINKING_BUDGET_TOKENS": "2048",  # 1024, 2048, 4096
        "ANTHROPIC_API_KEY": "your_anthropic_api_key_here"
    }
    
    print("=== Claude Thinking 配置示例 ===")
    for key, value in config_example.items():
        print(f"{key}={value}")
    
    print("\n=== 配置说明 ===")
    print("• SAG_THINKING_MODEL: Claude 模型名称")
    print("• SAG_THINKING_PROVIDER: 设置为 'anthropic'")
    print("• SAG_REASONING_EFFORT: 推理深度 (low/medium/high)")
    print("• SAG_THINKING_BUDGET_TOKENS: thinking 预算 token 数")
    print("• ANTHROPIC_API_KEY: Anthropic API 密钥")

def show_thinking_budget_mapping():
    """显示 reasoning_effort 到 budget_tokens 的映射"""
    
    print("\n=== Reasoning Effort 映射 ===")
    mapping = {
        "low": 1024,
        "medium": 2048, 
        "high": 4096
    }
    
    for effort, budget in mapping.items():
        print(f"• {effort} -> {budget} tokens")

def show_usage_example():
    """显示使用示例"""
    
    print("\n=== 使用示例 ===")
    print("1. 复制 .env.example 为 .env")
    print("2. 填入你的 ANTHROPIC_API_KEY")
    print("3. 设置 Claude thinking 相关配置")
    print("4. 运行 SAG:")
    print("   sag project https://github.com/example/project.git")

def main():
    """主函数"""
    print("🤖 Claude Thinking 功能示例")
    print("=" * 50)
    
    setup_claude_thinking_config()
    show_thinking_budget_mapping()
    show_usage_example()
    
    print("\n" + "=" * 50)
    print("💡 提示:")
    print("• Claude thinking 功能需要 Claude Sonnet 4 或更高版本")
    print("• 确保你的 API 密钥有足够的配额")
    print("• 可以通过 SAG_VERBOSE=true 查看详细的 thinking 过程")

if __name__ == "__main__":
    main() 