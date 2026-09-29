URL: https://arena-zshops-solved-quarterly.trycloudflare.com/v1 | API Key: Không cần (để trống hoặc none) | Model khả dụng: Text Embedding/Rerank gồm qwen3-0.6b, qwen3-4b, qwen3-8b (GPU 0) và Multimodal VL gồm qwen3-vl-2b, qwen3-vl-8b (GPU 1) | Gọi nhanh: curl -X POST "https://arena-zshops-solved-quarterly.trycloudflare.com/v1/embeddings" -H "Content-Type: application/json" -d '{"model":"qwen3-0.6b","input"🙁"Xin chào"]}' hoặc đổi endpoint sang /v1/rerank với body {"model":"qwen3-0.6b","query":"câu hỏi","documents"🙁"đoạn 1","đoạn 2"]}.


Base URL: https://trends-framing-gear-die.trycloudflare.com/v1
Playground UI & Docs: https://trends-framing-gear-die.trycloudflare.com/ui
 hoặc /docs
API Key: Không yêu cầu (để trống hoặc bất kỳ chuỗi nào)
Model ID: cuongnguyen1802/qwen38-27b-gguf (hoặc alias qwen3.8-27b-vlm)
Tự động tắt khi nhàn rỗi: 120 phút (IDLE_SHUTDOWN_MINUTES=120)
Ví dụ cURL Chat Completion:
bash


curl -X POST https://trends-framing-gear-die.trycloudflare.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cuongnguyen1802/qwen38-27b-gguf",
    "messages": [
      {"role": "user", "content": "Xin chào! Bạn là ai?"}
    ],
    "max_tokens": 128,
    "temperature": 0.7
  }'