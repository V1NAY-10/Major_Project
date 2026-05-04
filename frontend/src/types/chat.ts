export interface Message {
  id?: string;
  role: "user" | "assistant";
  content: string;
  code?: string;
  intents?: any[];
  preview?: any[];
  intent_response?: any;
}

export interface ChatState {
  messages: Message[];
  loading: boolean;
  error: string;
  successMsg: string;
}
