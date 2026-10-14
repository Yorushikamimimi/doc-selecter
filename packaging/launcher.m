#import <Cocoa/Cocoa.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>

static int openDirectory(NSString *path) {
    if (![path isAbsolutePath]) return -1;
    int fd = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
    for (NSString *part in [path pathComponents]) {
        if ([part isEqualToString:@"/"]) continue;
        if ([part isEqualToString:@".."] || [part isEqualToString:@"."]) { close(fd); return -1; }
        const char *name = [part fileSystemRepresentation];
        if (mkdirat(fd, name, 0700) < 0 && errno != EEXIST) { close(fd); return -1; }
        int child = openat(fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
        close(fd); fd = child;
        if (fd < 0) return -1;
    }
    return fd;
}

static void fail(NSString *message, BOOL diagnose) {
    const char *testRoot = getenv("DOCSELECTER_TEST_ROOT");
    NSString *base = testRoot && *testRoot ? [NSString stringWithUTF8String:testRoot] :
        [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/文件整理助手"];
    NSString *dir = [base stringByAppendingPathComponent:@"state"];
    int fd = openDirectory(dir);
    NSString *log = @"（诊断目录不可写）";
    if (fd >= 0) {
        int out = openat(fd, "app-launch.log", O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);
        close(fd);
        if (out >= 0) {
            dprintf(out, "%s\n", [message UTF8String]); fsync(out); close(out);
            log = [dir stringByAppendingPathComponent:@"app-launch.log"];
        }
    }
    fprintf(stderr, "%s\n诊断：%s\n", [message UTF8String], [log UTF8String]);
    if (!diagnose) {
        [NSApplication sharedApplication];
        NSAlert *alert = [[NSAlert alloc] init];
        alert.messageText = @"文件整理助手无法启动";
        alert.informativeText = [message stringByAppendingFormat:@"\n诊断记录：%@", log];
        [alert addButtonWithTitle:@"好"]; [alert runModal];
    }
}

int main(int argc, char **argv) {
    @autoreleasepool {
        BOOL diagnose = NO;
        for (int i = 1; i < argc; i++) if (strcmp(argv[i], "--diagnose") == 0) diagnose = YES;
        if (diagnose && (!getenv("DOCSELECTER_TEST_ROOT") || !*getenv("DOCSELECTER_TEST_ROOT"))) {
            fprintf(stderr, "诊断模式必须注入 DOCSELECTER_TEST_ROOT，未启动真实模式。\n"); return 64;
        }
        NSBundle *bundle = [NSBundle mainBundle];
        NSString *python = [bundle objectForInfoDictionaryKey:@"DocSelecterPython"];
        NSString *entry = [bundle pathForResource:@"bundle_entry" ofType:@"py"];
        if (!python || ![python isAbsolutePath] || ![[NSFileManager defaultManager] isExecutableFileAtPath:python]) {
            fail(@"找不到构建时指定的 Python 3.12.13。请恢复本机运行环境或重新构建；应用不会自动安装依赖。", diagnose); return 1;
        }
        if (!entry) { fail(@"应用资源不完整，请重新构建。", diagnose); return 1; }
        char **args = calloc((size_t)argc + 5, sizeof(char *));
        args[0] = (char *)[python fileSystemRepresentation]; args[1] = "-I"; args[2] = "-B";
        args[3] = (char *)[entry fileSystemRepresentation];
        int n = 4;
        for (int i = 1; i < argc; i++) if (strncmp(argv[i], "-psn_", 5) != 0) args[n++] = argv[i];
        args[n] = NULL;
        execv(args[0], args);
        free(args);
        fail(@"无法执行本机 Python，请检查运行环境和应用访问权限。", diagnose); return 1;
    }
}
