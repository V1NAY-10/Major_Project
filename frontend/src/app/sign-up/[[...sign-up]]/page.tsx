import { SignUp } from "@clerk/nextjs";

export default function Page() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="w-full max-w-md p-4">
        <div className="mb-8 text-center">
            <div className="inline-flex items-center justify-center w-12 h-12 bg-primary rounded-xl mb-4 shadow-lg shadow-primary/20">
                <span className="text-white text-2xl font-bold">Z</span>
            </div>
            <h1 className="text-2xl font-bold text-foreground">Create Account</h1>
            <p className="text-foreground/60 text-sm mt-1">Join FreeCAD AI to start building</p>
        </div>
        <SignUp appearance={{
            elements: {
                formButtonPrimary: 'bg-primary hover:bg-primary/90 transition-all text-sm normal-case',
                card: 'bg-card border border-border shadow-none',
                headerTitle: 'hidden',
                headerSubtitle: 'hidden',
                socialButtonsBlockButton: 'border-border hover:bg-foreground/5 text-foreground transition-all',
                formFieldLabel: 'text-foreground/70 font-medium',
                formFieldInput: 'bg-foreground/5 border-border focus:border-primary/50 text-foreground transition-all',
                footerActionLink: 'text-primary hover:text-primary/80 transition-all font-semibold',
                dividerLine: 'bg-border',
                dividerText: 'text-foreground/40'
            }
        }} />
      </div>
    </div>
  );
}
