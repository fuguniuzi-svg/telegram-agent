import os  
import logging  
from telegram import Update  
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes  
import dashscope  
logging.basicConfig(  
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',  
    level=logging.INFO  
)  
logger = logging.getLogger(__name__)  
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")  
if not DASHSCOPE_API_KEY:  
    raise ValueError("DASHSCOPE_API_KEY not set")  
conversation_history = {}  
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  
    user_id = update.effective_user.id  
    conversation_history[user_id] = []  
    await update.message.reply_text(  
        "👋 你好！我是 AsNiuzi123_bot，由阿里云通义千问驱动。\n\n"  
        "我可以帮你：\n"  
        "💬 对话和回答问题\n"  
        "💻 提供建议\n"  
        "📚 知识问答\n\n"  
        "直接发送消息开始聊天吧！"  
    )  
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  
    await update.message.reply_text(  
        "/start - 开始对话\n"  
        "/help - 显示帮助\n"  
        "/clear - 清除对话历史\n\n"  
        "直接发送消息即可与 AI 对话"  
    )  
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  
    user_id = update.effective_user.id  
    conversation_history[user_id] = []  
    await update.message.reply_text("✅ 对话历史已清除")  
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  
    user_id = update.effective_user.id  
    user_message = update.message.text  
      
    if user_id not in conversation_history:  
        conversation_history[user_id] = []  
      
    conversation_history[user_id].append({  
        "role": "user",  
        "content": user_message  
    })  
      
    try:  
        await update.message.chat.send_action("typing")  
          
        response = dashscope.Generation.call(  
            api_key=DASHSCOPE_API_KEY,  
            model="qwen-turbo",  
            messages=conversation_history[user_id]  
        )  
          
        logger.info(f"Response: {response}")  
          
        if response.status_code == 200:  
            assistant_message = response.output.text  
            conversation_history[user_id].append({  
                "role": "assistant",  
                "content": assistant_message  
            })  
              
            if len(assistant_message) > 4096:  
                for i in range(0, len(assistant_message), 4096):  
                    await update.message.reply_text(assistant_message[i:i+4096])  
            else:  
                await update.message.reply_text(assistant_message)  
        else:  
            await update.message.reply_text(f"❌ 错误: {response.message}")  
      
    except Exception as e:  
        logger.error(f"Error: {e}")  
        await update.message.reply_text(f"❌ 出错: {str(e)}")  
def main():  
    token = os.getenv("TELEGRAM_BOT_TOKEN")  
    if not token:  
        raise ValueError("TELEGRAM_BOT_TOKEN not set")  
      
    application = Application.builder().token(token).build()  
      
    application.add_handler(CommandHandler("start", start))  
    application.add_handler(CommandHandler("help", help_command))  
    application.add_handler(CommandHandler("clear", clear_command))  
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))  
      
    application.run_polling()  
if __name__ == '__main__':  
    main()